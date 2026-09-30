# Office public-IP updater

Phase AUTO-OFFICE-PUBLIC-IP-UPDATER.

When the office modem restarts it gets a new public address, and until
somebody edits the Allowed IP screen every employee is refused with *"Mạng
hiện tại của bạn không được phép dùng để chấm công."* This keeps the
whitelist current by itself.

One agent, on one fixed machine in the office, posts a signed request every
few minutes. The backend reads the public address **from that request** and
records it. Nothing about the address is taken from the agent — it proves it
is on the office network by being on it.

```
office PC ──HTTPS + HMAC──▶ Cloudflare ──▶ nginx ──▶ /api/internal/attendance/update-office-ip/
                                                         │
                                       reads CF-Connecting-IP, writes it as /32 (or /128)
```

## 1. Server side

Set three values in production's `.env` (the deployment environment — never
in the repository) and restart the service:

```
OFFICE_IP_UPDATER_SECRET=<a long random string>
OFFICE_IP_UPDATER_COMPANY_ID=<the company's numeric id>
# optional, shown with their defaults
OFFICE_IP_UPDATER_MAX_SKEW_SECONDS=120
OFFICE_IP_PREVIOUS_TTL_MINUTES=120
```

Generate the secret where it will be used, and paste it once:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

With `OFFICE_IP_UPDATER_SECRET` unset the endpoint answers 404 and nothing
about attendance changes. That is the correct state for any deployment not
running the agent.

The company id is the one on the Allowed IP screen
(`/attendance/attendance-rule-view/`). It is pinned on the server, not sent
by the agent, so a compromised office PC has no way to point this at another
company's whitelist.

## 2. Office machine

Choose a PC that is powered on during working hours and is on the office
network — not a laptop that goes home, and not on the guest Wi-Fi.

```powershell
# Machine-level, so a scheduled task running as SYSTEM can read them.
# Run this once, in an elevated PowerShell. Nothing is written to disk by the
# script itself.
[Environment]::SetEnvironmentVariable(
  'JOYDIGI_OFFICE_IP_URL',
  'https://checkin.joydigi.net/api/internal/attendance/update-office-ip/',
  'Machine')
[Environment]::SetEnvironmentVariable(
  'JOYDIGI_OFFICE_IP_SECRET', '<the same secret>', 'Machine')
```

Then check it works, in a **new** shell (environment variables are read at
process start):

```powershell
python C:\joydigi\office_ip_updater.py
# 2026-09-30 07:12:04 office-ip-updater: ok: status=rotated current=203.0.x.x previous_retained=True rule_enabled=True
```

`Last Run Result` / exit codes:

| code | meaning | what to do |
|---|---|---|
| 0 | recorded (changed, or already correct) | nothing |
| 1 | configuration missing | set the two environment variables |
| 2 | credential rejected, or endpoint disabled | check the secret matches the server's |
| 3 | backend unreachable after 3 tries | the next run retries; check the line if it persists |
| 4 | backend could not see our address | the machine is not reaching the site through Cloudflare |

### A note on the secret

Machine-level environment variables are readable by any local administrator.
That is an acceptable trade for a value whose only power is "assert that the
network this machine is on is the office", and it is why the server pins the
company and why the endpoint can do nothing else. If you want it out of the
environment entirely, store it with `Export-Clixml`/`Import-Clixml` under the
task account's profile (DPAPI-encrypted, readable only by that account) and
set `JOYDIGI_OFFICE_IP_SECRET` from it in a one-line wrapper. Do not commit
it anywhere, and do not put it in the task's arguments — command lines are
visible to every process on the machine.

## 3. Task Scheduler

Two triggers on one task: once at startup (the modem and the PC often
restart together) and every 10 minutes while it runs.

```powershell
# Elevated PowerShell. Runs as SYSTEM, whether or not anybody is logged in.
$action = New-ScheduledTaskAction -Execute 'C:\Windows\py.exe' `
  -Argument 'C:\joydigi\office_ip_updater.py'

$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = 'PT2M'   # let the network come up first

$every10 = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 10) `
  -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName 'JoyDigi Office IP Updater' `
  -Action $action -Trigger $atStartup, $every10 -Settings $settings `
  -User 'SYSTEM' -RunLevel Highest -Description `
  'Keeps the office public IP on the attendance whitelist.'
```

Check it:

```powershell
Start-ScheduledTask -TaskName 'JoyDigi Office IP Updater'
Get-ScheduledTaskInfo -TaskName 'JoyDigi Office IP Updater' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`-Execute 'C:\Windows\py.exe'` uses the Python launcher, which survives a
Python upgrade; point it at a specific `python.exe` if you prefer. Adjust the
script path to wherever you put the file.

## 4. What happens when things go wrong

**The agent stops running** (PC off, task disabled, secret rotated). The
whitelist is left exactly as it was, so everybody keeps checking in on the
last known good address. Nothing expires except the *previous* address, and
only after the current one has been confirmed. There is no state in which
this feature empties a list or turns a restriction on.

**The modem changes address while the agent is off.** Employees are refused
until the agent runs again — which it does at the next 10-minute tick, or 2
minutes after the PC next boots. The window is the gap, not the day.

**The backend is unreachable.** Three tries over about 20 seconds, then the
run ends and the schedule takes over. No unbounded loop, no growing backoff
that silently stops trying.

**Somebody replays a captured request from another network.** The nonce is
single-use on the server and the timestamp expires, so a replay is refused.
This matters precisely because the address is taken from the request: a
replay that *did* work would whitelist the replayer.

**The company has no IP rule yet.** One is created **disabled**, and the
agent says so. Enabling a network restriction is a decision for an
administrator on the Allowed IP screen, not something a background agent does
on its own.

## 5. What this does not do

It does not decide whether attendance is restricted by network — it only
maintains the list. It does not remove entries an administrator added by
hand, even one that happens to equal an address it also tracks. It does not
let the mobile app whitelist anything, and it does not trust an address sent
in a request body.
