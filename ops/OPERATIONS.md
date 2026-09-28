# Garmin MCP free-plan operations

`ops/keepalive.yml` is an inactive template. Nothing in this directory installs a
monitor, changes Render, calls Garmin, or starts a GitHub schedule. Keep the
repository variable `RENDER_FREE_HOURS_VERIFIED` unset for now: the Render service
inventory has not been verified, and waking the old deployment may initiate Garmin
authentication.

## Prerequisites before enabling keepalive

1. Deploy the reviewed server patch through the normal authorized deployment
   process. Establish that `/health` only reports local state and that startup
   cannot trigger a password login or token exchange. Test this locally with
   outbound Garmin access blocked; inspect the deployed code/configuration before
   sending the first live wake-up request.
2. In the Render Dashboard, inspect the correct workspace, all running free web
   services, and monthly free-instance-hour usage. Enable this always-on monitor
   only after confirming this is the workspace's sole free instance and that
   remaining hours cover the rest of the month. Include hours already consumed by
   other or removed services. Repeat the check if another free service is added.
3. Confirm the target GitHub repository is public and uses standard hosted Linux
   runners. Do not make a private repository public for this purpose. The workflow
   gate intentionally skips private repositories.
4. Copy `ops/keepalive.yml` into `.github/workflows/garmin-keepalive.yml` on the
   chosen public repository's default branch. Review that repository's existing
   push/deploy workflows before committing. Keep the gate unset during review.
5. After the prerequisites are satisfied, create the repository Actions variable
   `RENDER_FREE_HOURS_VERIFIED` with the exact value `true`. Run the workflow once
   using **Run workflow**, then inspect its execution history.

The schedule runs at minutes 3, 13, 23, 33, 43, and 53 of each hour. Each execution
makes one HTTPS request to `/health`, with a 15-second connection timeout and an
85-second total timeout. It has no retries and never calls a Garmin tool. Overlaps
are cancelled, and the whole job has a three-minute timeout. A successful run
means the process answered `ok: true`; it does not verify Garmin authentication or
data freshness. The complete health response is not printed in CI logs.

To disable the monitor, unset the variable or set it to `false`, and cancel any
currently running job. A disabled gate cannot recover an already sleeping service.

## Free-plan limits and alternatives

Render's free web services sleep after 15 minutes without incoming HTTP requests
or WebSocket messages. The 750 free instance hours are shared by a **workspace**
per calendar month. One continuously running service uses 720 hours in a 30-day
month or 744 in a 31-day month. Render can restart a free service at any time, and
its local filesystem does not survive sleep/restarts/redeploys. Keep durable token
storage working even when keepalive is installed. Bandwidth and build quotas are
separate. [Render free service documentation](https://render.com/docs/free)

GitHub schedules can be delayed or dropped. Public-repository schedules are
disabled after 60 days without repository activity. The offset reduces exposure
to busy hourly boundaries but cannot guarantee a request every ten minutes.
[GitHub scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Standard GitHub-hosted runners are free for public repositories. In private
repositories, each job rounds up to a whole minute; this schedule produces
4,320–4,464 runs in 30–31 days, exceeding the 2,000 monthly minutes included with
GitHub Free and the 3,000 with Pro even if each job finishes within a minute.
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
and [execution-minute rounding](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-job-execution-time)

If an existing UptimeRobot account is available, a free HTTP monitor with a
five-minute interval is an alternative. Apply the same safe-health and Render-hour
prerequisites before creating it. Use one primary monitor and verify its history;
do not install both mechanisms without an operational reason. Free monitoring does
not provide a guarantee of uninterrupted service.
[UptimeRobot plan details](https://uptimerobot.com/pricing/)

## Tokens and 429 recovery

GitHub **secret** Gists are unlisted, not private: anyone with the URL can read
them, including their version history. Store only authenticated ciphertext there,
with the encryption key kept separately in Render environment variables. Never
upload a plaintext Garmin token dump, password, or encryption key to a Gist or
repository. Base64 is encoding, not encryption.
[GitHub Gist visibility](https://docs.github.com/en/get-started/writing-on-github/editing-and-sharing-content-with-gists/creating-gists)

An error at `oauth/exchange` identifies a failed token exchange, not necessarily a
fresh password login. A locally cached `last_error` disappearing does not prove
that Garmin's rate limit has lifted. A process restart can reset local state.
Preserve rate-limit timestamps and cooldown across restarts; respect a supplied
`Retry-After`. The HTTP standard does not specify Garmin's rate-limit window or
account/IP policy. Do not repeatedly probe login to discover whether it recovered.
[HTTP 429 semantics](https://datatracker.ietf.org/doc/html/rfc6585#section-4)

If a new seed is required, perform the single local login only under the approved
recovery procedure after the applicable cooldown. Password login must not run on
Render. Keep plaintext seed material out of shell history, command arguments, logs,
and Git. A token loaded from storage is not proof of a recent successful API call.

## Evidence needed before changing the consumer's wake-up instructions

Retain the existing Claude scheduled-task wake-up step until live validation is
complete. During the current authentication incident, do not use that step to wake
the old deployment if startup can attempt authentication.

After the patch and monitor are active, collect:

- A live health response from the patched server with restored-token source and
  cooldown diagnostics, without exposing tokens.
- A controlled restart/redeploy showing token restoration and an unchanged
  `last_login_at`. No password login or token exchange may be caused by the check.
- At least 30 minutes of monitor execution history and health latency observations.
  A single sub-second response does not prove future availability.
- One successful authorized Garmin data request after cooldown, without repeated
  login attempts; check that any refreshed tokens were persisted.

Only then replace the unconditional wake-up step with a bounded connection-retry
instruction that tolerates occasional Render restarts. Preserve the rule that
authentication failure or cooldown must never trigger repeated login attempts.
