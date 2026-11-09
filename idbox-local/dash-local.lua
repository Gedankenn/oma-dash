#!/usr/bin/lua
-- dash-local: iDBox V2 local telemetry -> JSON (CGI)
io.write("Content-Type: application/json\r\nCache-Control: no-store\r\n\r\n")

local function slurp(p)
  local f = io.open(p, "r")
  if not f then return "" end
  local s = f:read("*a"); f:close()
  return s or ""
end

local function pct(a, b) if not b or b == 0 then return 0 end return math.floor(a / b * 1000) / 10 end
local function num(x) return tonumber(x) or 0 end

-- uptime / load
local up = math.floor(num((slurp("/proc/uptime"):match("^(%d+)"))))
local l1, l2, l3 = slurp("/proc/loadavg"):match("([%d%.]+)%s+([%d%.]+)%s+([%d%.]+)")

-- cpu from /proc/stat delta
local u, n, s, idle, iow, irq, sirq = slurp("/proc/stat"):match("^cpu%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)")
u, n, s, idle, iow, irq, sirq = num(u), num(n), num(s), num(idle), num(iow), num(irq), num(sirq)
local total = u + n + s + idle + iow + irq + sirq
local idleall = idle + iow
local cpu = 0
local pt, pi = slurp("/tmp/.dash_cpu"):match("(%d+)%s+(%d+)")
if pt then
  pt, pi = num(pt), num(pi)
  if pt > 0 and total > pt then
    cpu = math.floor(100 * ((total - pt) - (idleall - pi)) / (total - pt))
  end
end
local fh = io.open("/tmp/.dash_cpu", "w")
if fh then fh:write(total .. " " .. idleall .. "\n"); fh:close() end

-- memory
local memt = num(slurp("/proc/meminfo"):match("MemTotal:%s+(%d+)"))
local mema = num(slurp("/proc/meminfo"):match("MemAvailable:%s+(%d+)"))

-- temp
local temp = num((slurp("/sys/class/thermal/thermal_zone0/temp"):gsub("%s", "")))

-- uptime already; procs
local np = 0
local p = io.popen("ls /proc 2>/dev/null")
if p then
  for l in p:lines() do if l:match("^%d+$") then np = np + 1 end end
  p:close()
end

-- disks (kB)
local function df(mnt)
  local t, uu, a = 0, 0, 0
  local q = io.popen("df -k " .. mnt .. " 2>/dev/null | tail -n 1")
  if q then
    for line in q:lines() do
      local f1, f2, f3 = line:match("^%S+%s+(%d+)%s+(%d+)%s+(%d+)")
      t, uu, a = num(f1), num(f2), num(f3)
    end
    q:close()
  end
  return t * 1024, uu * 1024, a * 1024
end
local rt, ru = df("/")
local ut, uu = df("/mnt/fs_user")

-- network eth0
local rx, tx = 0, 0
for line in slurp("/proc/net/dev"):gmatch("[^\n]+") do
  if line:match("eth0:") then
    local d = line:match("eth0:%s*(.+)")
    local vals = {}
    for v in d:gmatch("%d+") do vals[#vals + 1] = tonumber(v) end
    rx, tx = vals[1] or 0, vals[9] or 0
  end
end

local host = slurp("/proc/sys/kernel/hostname"):gsub("%s", "")
local fw = slurp("/etc/version"):gsub("%s", "")

io.write(string.format(
  '{"ts":%d,"host":"%s","fw":"%s","uptime":%d,"load":[%s,%s,%s],' ..
  '"cpu":%d,"mem":{"total":%d,"used":%d},"temp":%d,' ..
  '"disk":{"root":{"total":%d,"used":%d},"user":{"total":%d,"used":%d}},' ..
  '"net":[%d,%d],"procs":%d}\n',
  os.time(), host, fw, up, l1 or "0", l2 or "0", l3 or "0",
  cpu, memt * 1024, (memt - mema) * 1024, temp,
  rt, ru, ut, uu, rx, tx, np))
