--[[
ControllerLog optimizer bot for BizHawk (EmuHawk 2.6.2 or newer).

Python (`controllerlog optimize`, controllerlog/optimize.py) sends commands over
BizHawk's socket IPC; this script runs them on the loaded core and replies. It
lets the optimizer try thousands of input candidates on BizHawk's own core, so
the inputs it finds sync when the exported .bk2 is played back.

Start the Python side FIRST, then EmuHawk:

  EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=43880 --movie="in.bk2"
              --lua="tools\bizhawk\controllerlog_bot.lua" "path\to\rom.gbc"

EmuHawk connects to the socket while it starts up and refuses to start if
nothing is listening. The --socket flags only exist on the command line. If
EmuHawk is already running with them, (re)start this script from
Tools > Lua Console: it reconnects, so one EmuHawk can serve several sessions.

Protocol: BizHawk frames every message as "<byte length> <payload>" (2.6.2+).
One command per message, one reply per command: "OK ..." or "ERR <message>".
On start the script sends "HELLO <system id> <frame> <movie mode> <protocol>".

  PING                              -> OK pong
  KEYS <LogKey>                     -> OK <names>   e.g. KEYS #Up|Down|Left|Right|Start|Select|B|A|Power|
  DOMAINS                           -> OK <name>:<size hex> ...   (names %-encoded)
  SPEED max | <percent>             -> OK   (max = unthrottled; percent = integer 1..6400;
                                             the user's settings are restored on exit)
  FRAME                             -> OK <framecount>
  SAVE                              -> OK <id>        (in-memory core savestate)
  LOAD <id>                         -> OK <framecount>
  FREE <id>                         -> OK
  RUN <n> [<lines>]                 -> OK <framecount> <lag frames>
  READ <read>...                    -> OK <v>...
  EVAL <id> <read>... [UNTIL <read> <op> <value>] | <lines>
                                    -> OK <framecount> <lag> [<hit>] <v>...
  BRANCH <id> | <lines>             -> OK <new id> <framecount> <lag>
  SEEK <frame>                      -> OK <framecount> <movie length>
  QUIT                              -> OK bye   (the script then ends)

<lines>: bk2 input-log lines ("|.......A.|") joined by ';'. "N*line" repeats a
line N times. RUN holds the last line when given fewer than n lines (all
buttons released if none; that needs KEYS).
<read>: "<domain> <addr hex> <size> <le|be>" with the domain %-encoded
(System%20Bus). Size is 1, 2 or 4, s1/s2/s4 for signed, or h<len> for the
SHA-256 of <len> bytes (memory.hash_region).
UNTIL: stop as soon as "<read> <op> <value>" holds (ops == != < <= > >= and &
for "any mask bit set") and report after how many frames (<hit>, -1 = never).
EVAL/BRANCH load the savestate first; one EVAL = one candidate = one round trip.
SEEK needs a movie in PLAY mode (EmuHawk --movie): the movie plays up to
<frame>, then the script stops it so joypad input takes over.

Frame stepping (how the script avoids freezing the GUI and never advances
frames while idle): EmuHawk resumes a script that called emu.yield() once per
GUI loop iteration, even while paused, but resumes one that called
emu.frameadvance() only after a frame was emulated, which never happens while
paused. So the script keeps the emulator PAUSED whenever it isn't running
frames and idles with emu.yield(), polling the socket with a short timeout.
The GUI stays responsive and no frame is emulated. RUN/EVAL/SEEK/BRANCH
unpause, set input and call emu.frameadvance() once per frame, then pause
again. After a reply the script waits BURST_WAIT_MS for the next command
without yielding, so back-to-back commands from the optimizer don't pay a GUI
loop iteration each. It still yields at least every MAX_FRAMELESS_BURST
commands that emulate no frames.

Input: joypad.setfrommnemonicstr() only fills BizHawk's override adapter,
which is copied into the controller in RunControllerChain. That step runs
BEFORE emu.yield() scripts are resumed, and the adapter is cleared right
before the next frame, so a line set just after an idle yield would be lost
for that frame. joypad.set() applies immediately. So when KEYS gave the
column layout the script decodes each line and calls joypad.set() with every
button (setfrommnemonicstr is still called first for axis values). Without
KEYS it falls back to setfrommnemonicstr. The Python side always sends KEYS.

Safety: frame-advancing code never runs inside pcall (Lua 5.1 can't yield
across pcall); only parsing, input, reads and replies are protected. Memory
domains and addresses are validated, because BizHawk silently reads the
current domain for unknown names and returns 0 out of range. RUN/EVAL/BRANCH
refuse to run while a movie is active: playback ignores joypad input and
recording would record it.
]]

local PROTO = 1
local RECONNECT_ON_START = true   -- reconnect so the script can be restarted for a new session
local IDLE_POLL_MS = 5            -- socket timeout per GUI loop iteration while idle
local BURST_WAIT_MS = 50          -- wait this long for the next command before idling
local MAX_FRAMELESS_BURST = 64
local MAX_FRAMES = 2000000        -- per command

local running = true
local states = {}                 -- int id -> BizHawk savestate GUID
local next_state = 1
local KEY = nil                   -- {groups = {{names}}, names = {...}}
local NEUTRAL = nil
local line_cache, line_cache_n = {}, 0
local domain_sizes = nil
local pad_names = nil
local saved_cfg = nil

local function log(msg) print("[controllerlog_bot] " .. msg) end

local function clean(e)
  local s = tostring(e)
  return (s:gsub("[\r\n]+", " "))
end

local function fail(msg) error(msg, 0) end

local function fmtint(v)
  if math.type and math.type(v) == "integer" then return tostring(v) end
  return string.format("%.0f", v)
end

local function fmtval(v)
  if type(v) == "string" then return v end
  return fmtint(v)
end

local band
do
  local ld = loadstring or load
  local f = ld("return function(a, b) return a & b end")
  if f then
    band = f()
  elseif bit32 then
    band = bit32.band
  elseif bit then
    band = bit.band
  else
    band = function(a, b)
      local r, m = 0, 1
      while a > 0 and b > 0 do
        local x, y = a % 2, b % 2
        if x == 1 and y == 1 then r = r + m end
        a, b, m = (a - x) / 2, (b - y) / 2, m * 2
      end
      return r
    end
  end
end

local function pct_decode(s)
  return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function pct_encode(s)
  return (s:gsub("[%%%s:;|%c]", function(c) return string.format("%%%02X", string.byte(c)) end))
end

local function tokens(s)
  local t = {}
  for w in s:gmatch("%S+") do t[#t + 1] = w end
  return t
end

-- emulator control ----------------------------------------------------------

local function pause()
  if not client.ispaused() then client.pause() end
end

local function advance()           -- emulate exactly one frame (yields; never inside pcall)
  if client.ispaused() then client.unpause() end
  emu.frameadvance()
end

local function save_config()
  if saved_cfg then return end
  saved_cfg = {}
  pcall(function()
    local c = client.getconfig()
    saved_cfg.throttle = c.ClockThrottle
    saved_cfg.speed = c.SpeedPercent
    saved_cfg.unthrottled = c.Unthrottled
  end)
end

-- Config.Unthrottled is what EmuHawk's "Toggle Throttle" hotkey sets. Unlike
-- emu.limitframerate(false) (ClockThrottle), it also overrides sound/vsync
-- throttling. There is no Lua function for it, so it is set on client.getconfig().
local function set_unthrottled(v)
  pcall(function() client.getconfig().Unthrottled = v end)
end

local function restore_config()
  if not saved_cfg then return end
  if saved_cfg.unthrottled ~= nil then set_unthrottled(saved_cfg.unthrottled) end
  if saved_cfg.throttle ~= nil then pcall(emu.limitframerate, saved_cfg.throttle) end
  if saved_cfg.speed ~= nil then pcall(client.speedmode, saved_cfg.speed) end
  saved_cfg = nil
end

local function movie_mode()
  local ok, m = pcall(movie.mode)
  if ok and m then return m end
  return "INACTIVE"
end

local function require_no_movie()
  local m = movie_mode()
  if m ~= "INACTIVE" then
    fail("a movie is loaded (mode " .. m .. "): joypad input would be ignored or recorded. "
      .. "Use SEEK, or stop the movie first (File > Movie > Stop Movie)")
  end
end

-- memory ---------------------------------------------------------------------

local function domains()
  if domain_sizes then return domain_sizes end
  local d, order = {}, {}
  local list = memory.getmemorydomainlist()
  if type(list) == "string" then
    for name in list:gmatch("[^\r\n]+") do order[#order + 1] = name end
  else
    local i = 0
    while list[i] ~= nil do order[#order + 1] = list[i]; i = i + 1 end   -- BizHawk: 0-indexed
    if #order == 0 then for _, name in ipairs(list) do order[#order + 1] = name end end
  end
  for _, name in ipairs(order) do d[name] = memory.getmemorydomainsize(name) end
  domain_sizes = {sizes = d, order = order}
  return domain_sizes
end

local SIGNED_LIMIT = {[1] = 128, [2] = 32768, [4] = 2147483648}

local function parse_read(tok, i)
  local dom, addr_s, size_s, endian = tok[i], tok[i + 1], tok[i + 2], tok[i + 3]
  if not endian then fail("incomplete read spec (need <domain> <addr_hex> <size> <le|be>)") end
  local domain = pct_decode(dom)
  local dsize = domains().sizes[domain]
  if not dsize then fail("unknown memory domain '" .. domain .. "'") end
  local addr = tonumber((addr_s:gsub("^0[xX]", "")), 16)
  if not addr then fail("bad address '" .. addr_s .. "'") end
  local r = {domain = domain, addr = addr}
  local n
  local hl = size_s:match("^[hH](%d+)$")
  if hl and tonumber(hl) > 0 then
    r.hash = tonumber(hl)
    n = r.hash
  else
    local s, digits = size_s:match("^([sS]?)([124])$")
    if not digits then fail("bad size '" .. size_s .. "' (1, 2, 4, s1, s2, s4 or h<len>)") end
    if endian ~= "le" and endian ~= "be" then fail("bad endian '" .. endian .. "' (le or be)") end
    r.size, r.signed, r.be = tonumber(digits), s ~= "", endian == "be"
    n = r.size
  end
  if addr + n > dsize then
    fail(string.format("address 0x%X+%d outside %s (size 0x%X)", addr, n, domain, dsize))
  end
  return r, i + 4
end

local function do_read(r)
  if r.hash then return memory.hash_region(r.addr, r.hash, r.domain) end
  local v
  if r.size == 1 then
    v = memory.read_u8(r.addr, r.domain)
  elseif r.size == 2 then
    if r.be then v = memory.read_u16_be(r.addr, r.domain) else v = memory.read_u16_le(r.addr, r.domain) end
  else
    if r.be then v = memory.read_u32_be(r.addr, r.domain) else v = memory.read_u32_le(r.addr, r.domain) end
  end
  if r.signed then
    local lim = SIGNED_LIMIT[r.size]
    if v >= lim then v = v - 2 * lim end
  end
  return v
end

local OPS = {
  ["=="] = function(a, b) return a == b end,
  ["!="] = function(a, b) return a ~= b end,
  ["<"] = function(a, b) return a < b end,
  ["<="] = function(a, b) return a <= b end,
  [">"] = function(a, b) return a > b end,
  [">="] = function(a, b) return a >= b end,
  ["&"] = function(a, b) return band(a, b) ~= 0 end,
}

local function parse_until(tok, i)
  local r
  r, i = parse_read(tok, i)
  if r.hash then fail("UNTIL needs a numeric read, not a hash") end
  local op, val = tok[i], tok[i + 1]
  if not val then fail("UNTIL needs <op> <value>") end
  if not OPS[op] then fail("bad UNTIL operator '" .. op .. "'") end
  local v = tonumber(val)
  if not v then fail("bad UNTIL value '" .. val .. "'") end
  return {read = r, test = OPS[op], value = v}, i + 2
end

local function check(cond) return cond.test(do_read(cond.read), cond.value) end

-- input lines ------------------------------------------------------------------

local function pad_name_set()
  if pad_names ~= nil then return pad_names or nil end
  local ok, t = pcall(joypad.get)
  if ok and type(t) == "table" then
    pad_names = {}
    for k in pairs(t) do pad_names[k] = true end
  else
    pad_names = false
  end
  return pad_names or nil
end

local function parse_line(raw)
  local p = line_cache[raw]
  if p then return p end
  if raw:sub(1, 1) ~= "|" or raw:sub(-1) ~= "|" or #raw < 2 then
    fail("input line must look like |...|, got '" .. raw .. "'")
  end
  p = {raw = raw}
  if KEY then
    local segs = {}
    for seg in raw:sub(2):gmatch("([^|]*)|") do segs[#segs + 1] = seg end
    if #segs < #KEY.groups then
      fail("input line has " .. #segs .. " group(s), the LogKey has " .. #KEY.groups .. ": '" .. raw .. "'")
    end
    local set, has_axes = {}, false
    for gi, names in ipairs(KEY.groups) do
      local parts = {}
      for piece in (segs[gi] .. ","):gmatch("([^,]*),") do parts[#parts + 1] = piece end
      local n_axes = #parts - 1
      if n_axes > #names then fail("too many axis values in '" .. raw .. "'") end
      if n_axes > 0 then has_axes = true end
      local chars = parts[#parts]
      local nb = #names - n_axes
      if #chars < nb then fail("input line too short for the LogKey: '" .. raw .. "'") end
      for bi = 1, nb do set[names[n_axes + bi]] = chars:sub(bi, bi) ~= "." end
    end
    local pn = pad_name_set()
    if pn then
      for name in pairs(set) do
        if not pn[name] then
          fail("button '" .. name .. "' is not on this core's controller (wrong system or LogKey?)")
        end
      end
    end
    p.buttons, p.has_axes = set, has_axes
  end
  if line_cache_n > 20000 then line_cache, line_cache_n = {}, 0 end
  line_cache[raw] = p
  line_cache_n = line_cache_n + 1
  return p
end

local function parse_lines(text)
  local out = {}
  text = (text:gsub("^%s+", ""):gsub("%s+$", ""))
  if text == "" then return out end
  for item in (text .. ";"):gmatch("([^;]*);") do
    local cnt, body = item:match("^(%d+)%*(.*)$")
    local n = 1
    if cnt then n, item = tonumber(cnt), body end
    if n < 1 then fail("bad repeat count in '" .. item .. "'") end
    if #out + n > MAX_FRAMES then fail("too many frames (max " .. MAX_FRAMES .. ")") end
    local p = parse_line(item)
    for _ = 1, n do out[#out + 1] = p end
  end
  return out
end

local function apply_line(p)
  if p.buttons then
    if p.has_axes then joypad.setfrommnemonicstr(p.raw) end
    joypad.set(p.buttons)          -- every button, true or false: overrides physical input now
  else
    joypad.setfrommnemonicstr(p.raw)
  end
end

-- command handlers -----------------------------------------------------------------
-- A handler returns a reply string, or a job table {frames, n, hold, cond, seek, finish}
-- whose frames are then run by exec_job outside pcall.

local H = {}

local function state_guid(tok)
  local id = tonumber(tok or "")
  local g = id and states[id]
  if not g then fail("unknown state id '" .. tostring(tok) .. "'") end
  return g, id
end

function H.PING() return "OK pong" end

function H.FRAME() return "OK " .. fmtint(emu.framecount()) end

function H.SAVE()
  local g = memorysavestate.savecorestate()
  if g == nil or g == "" then fail("savecorestate failed") end
  local id = next_state
  next_state = next_state + 1
  states[id] = g
  return "OK " .. id
end

function H.LOAD(rest)
  local g = state_guid(tokens(rest)[1])
  memorysavestate.loadcorestate(g)
  return "OK " .. fmtint(emu.framecount())
end

function H.FREE(rest)
  local g, id = state_guid(tokens(rest)[1])
  memorysavestate.removestate(g)
  states[id] = nil
  return "OK"
end

function H.KEYS(rest)
  local key = rest:gsub("^%s+", ""):gsub("%s+$", "")
  if key:sub(1, 7) == "LogKey:" then key = key:sub(8) end
  local groups, names = {}, {}
  for chunk in key:gmatch("[^#]+") do
    local g = {}
    for name in chunk:gmatch("[^|]+") do g[#g + 1] = name; names[#names + 1] = name end
    if #g > 0 then groups[#groups + 1] = g end
  end
  if #names == 0 then fail("KEYS needs a LogKey like #Up|Down|...|") end
  local pn = pad_name_set()
  if pn then
    for _, name in ipairs(names) do
      if not pn[name] then
        fail("button '" .. name .. "' is not on this core's controller (wrong system or LogKey?)")
      end
    end
  end
  KEY = {groups = groups, names = names}
  line_cache, line_cache_n = {}, 0
  local set = {}
  for _, name in ipairs(names) do set[name] = false end
  NEUTRAL = {raw = nil, buttons = set, has_axes = false}
  return "OK " .. #names
end

function H.DOMAINS()
  local d = domains()
  local parts = {}
  for _, name in ipairs(d.order) do
    parts[#parts + 1] = pct_encode(name) .. ":" .. string.format("%X", d.sizes[name])
  end
  return "OK " .. table.concat(parts, " ")
end

function H.SPEED(rest)
  local arg = (tokens(rest)[1] or ""):lower()
  save_config()
  if arg == "max" then
    emu.limitframerate(false)
    set_unthrottled(true)
  else
    local pctv = tonumber(arg)
    if not pctv or pctv < 1 or pctv > 6400 or pctv ~= math.floor(pctv) then
      fail("bad SPEED '" .. arg .. "' (max or 1..6400)")
    end
    set_unthrottled(false)
    emu.limitframerate(true)
    client.speedmode(pctv)
  end
  return "OK"
end

function H.READ(rest)
  local tok = tokens(rest)
  if #tok == 0 then fail("READ needs at least one read spec") end
  local vals, i = {}, 1
  while i <= #tok do
    local r
    r, i = parse_read(tok, i)
    vals[#vals + 1] = fmtval(do_read(r))
  end
  return "OK " .. table.concat(vals, " ")
end

function H.RUN(rest)
  rest = rest:gsub("^%s+", "")
  local n_s, text = rest:match("^(%S+)%s?(.*)$")
  local n = tonumber(n_s or "")
  if not n or n < 0 or n ~= math.floor(n) then fail("RUN needs a frame count, got '" .. tostring(n_s) .. "'") end
  if n > MAX_FRAMES then fail("too many frames (max " .. MAX_FRAMES .. ")") end
  local frames = parse_lines(text or "")
  if #frames > n then fail("RUN " .. n .. " got " .. #frames .. " lines") end
  local hold = frames[#frames] or NEUTRAL
  if n > 0 and hold == nil then fail("RUN without lines needs KEYS (to build a neutral input)") end
  require_no_movie()
  return {frames = frames, n = n, hold = hold, finish = function(res)
    return "OK " .. fmtint(emu.framecount()) .. " " .. res.lag
  end}
end

local function parse_eval(cmd, rest)
  local head, text = rest:match("^([^|]*)|(.*)$")
  if not head then fail(cmd .. " needs ' | ' before the input lines") end
  local tok = tokens(head)
  if #tok == 0 then fail(cmd .. " needs a state id") end
  local g = state_guid(tok[1])
  local reads, cond, i = {}, nil, 2
  if cmd == "BRANCH" and #tok > 1 then fail("BRANCH takes only a state id") end
  while i <= #tok do
    if tok[i]:upper() == "UNTIL" then
      if cond then fail("only one UNTIL per EVAL") end
      cond, i = parse_until(tok, i + 1)
    else
      local r
      r, i = parse_read(tok, i)
      reads[#reads + 1] = r
    end
  end
  local frames = parse_lines(text)
  require_no_movie()
  return g, reads, cond, frames
end

function H.EVAL(rest)
  local g, reads, cond, frames = parse_eval("EVAL", rest)
  memorysavestate.loadcorestate(g)
  return {frames = frames, n = #frames, cond = cond, finish = function(res)
    local parts = {fmtint(emu.framecount()), tostring(res.lag)}
    if cond then parts[#parts + 1] = tostring(res.hit or -1) end
    for _, r in ipairs(reads) do parts[#parts + 1] = fmtval(do_read(r)) end
    return "OK " .. table.concat(parts, " ")
  end}
end

function H.BRANCH(rest)
  local g, _, _, frames = parse_eval("BRANCH", rest)
  memorysavestate.loadcorestate(g)
  return {frames = frames, n = #frames, finish = function(res)
    local reply = H.SAVE()
    return "OK " .. reply:sub(4) .. " " .. fmtint(emu.framecount()) .. " " .. res.lag
  end}
end

function H.SEEK(rest)
  local target = tonumber(tokens(rest)[1] or "")
  if not target or target < 0 or target ~= math.floor(target) then
    fail("SEEK needs a frame number, got '" .. rest .. "'")
  end
  if movie_mode() ~= "PLAY" then fail("SEEK needs a movie loaded in BizHawk (start EmuHawk with --movie)") end
  local length = movie.length()
  local fc = emu.framecount()
  if target < fc then fail("already at frame " .. fmtint(fc) .. ", past " .. fmtint(target) .. "; restart the movie") end
  if target > length then fail("movie has only " .. fmtint(length) .. " frames") end
  return {n = target - fc, finish = function()
    if emu.framecount() ~= target then fail("movie playback stopped at frame " .. fmtint(emu.framecount())) end
    movie.stop(false)
    return "OK " .. fmtint(emu.framecount()) .. " " .. fmtint(length)
  end}
end

function H.QUIT()
  running = false
  return "OK bye"
end

local function exec_job(job)
  local lag, hit, ran, err = 0, nil, 0, nil
  local cond = job.cond
  if cond then
    local ok, r = pcall(check, cond)
    if not ok then return "ERR " .. clean(r) end
    if r then hit = 0 end
  end
  while hit == nil and ran < job.n do
    if job.frames then
      local ok, e = pcall(apply_line, job.frames[ran + 1] or job.hold)
      if not ok then err = e; break end
    end
    advance()
    ran = ran + 1
    if emu.islagged() then lag = lag + 1 end
    if cond then
      local ok, r = pcall(check, cond)
      if not ok then err = r; break end
      if r then hit = ran end
    end
  end
  pause()
  if err then return "ERR " .. clean(err) end
  local ok, reply = pcall(job.finish, {lag = lag, hit = hit, ran = ran})
  if not ok then return "ERR " .. clean(reply) end
  return reply
end

local function dispatch(msg)
  local cmd, rest = msg:match("^%s*(%S+)%s?(.*)$")
  if not cmd then return "ERR empty command", false end
  cmd = cmd:upper()
  local h = H[cmd]
  if not h then return "ERR unknown command '" .. cmd .. "'", false end
  local ok, r = pcall(h, rest or "")
  if not ok then return "ERR " .. clean(r), false end
  if type(r) == "table" then return exec_job(r), true end
  return r, false
end

-- session ------------------------------------------------------------------------------

local function cleanup()
  for id, g in pairs(states) do pcall(memorysavestate.removestate, g); states[id] = nil end
  restore_config()
end

local function send(msg)
  local ok, n = pcall(comm.socketServerSend, msg)
  return ok and n ~= nil and n > 0
end

local function start()
  if comm == nil or comm.socketServerSend == nil then
    log("this BizHawk has no comm.socketServer* API; use EmuHawk 2.6.2 or newer")
    return false
  end
  local info = comm.socketServerGetInfo()
  if info == nil or info == "" then
    log("EmuHawk was started without --socket_ip/--socket_port. Start `controllerlog optimize ...` first, "
      .. "then EmuHawk with the command line it prints.")
    return false
  end
  if RECONNECT_ON_START then
    local ok, e = pcall(function() comm.socketServerSetPort(comm.socketServerGetPort()) end)
    if not ok then log("could not reconnect to " .. info .. ": " .. clean(e)) end
  end
  comm.socketServerSetTimeout(IDLE_POLL_MS)      -- after reconnecting: a new socket has no timeout
  save_config()
  local was_paused = client.ispaused()
  pause()
  local hello = string.format("HELLO %s %s %s %d", tostring(emu.getsystemid()), fmtint(emu.framecount()),
    movie_mode(), PROTO)
  if not send(hello) then
    log("could not reach the ControllerLog optimizer at " .. info .. ". Start `controllerlog optimize ...` "
      .. "first, then restart this script.")
    restore_config()
    if not was_paused then client.unpause() end
    return false
  end
  log("connected to " .. info .. " (" .. tostring(emu.getsystemid()) .. ", frame " .. fmtint(emu.framecount()) .. ")")
  return true
end

local function main_loop()
  local hot, frameless = false, 0
  while running do
    local msg = comm.socketServerResponse()
    if msg ~= nil and msg ~= "" then
      local reply, advanced = dispatch(msg)
      if not send(reply) then log("connection lost while replying; stopping"); break end
      if not hot then comm.socketServerSetTimeout(BURST_WAIT_MS); hot = true end
      if advanced then frameless = 0 else frameless = frameless + 1 end
      if running and frameless >= MAX_FRAMELESS_BURST then
        frameless = 0
        pause()
        emu.yield()
      end
    else
      if hot then comm.socketServerSetTimeout(IDLE_POLL_MS); hot = false; frameless = 0 end
      pause()
      emu.yield()
    end
  end
end

if event and event.onexit then pcall(event.onexit, cleanup) end
if start() then
  main_loop()
  cleanup()
  log("session ended; the emulator is left paused")
end
