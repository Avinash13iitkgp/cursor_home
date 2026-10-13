# Demo

One command starts the two daemon hosts, the supervisor, and the backend that serves the page. The failure demos kill a host, the backend, or both, while a task is in the middle of tool calls. Work continues from SQLite and the session files under `data/cow/`.

Run everything from the `coding-agent` directory.

## Start

`CURSOR_API_KEY` must be set in `.env`. Docker must be running.

```bash
./scripts/start.sh
```

Leave that terminal open. Ctrl+C stops the backend and the supervisor. The Docker hosts keep running.

When the script prints the URLs:

- Page: http://127.0.0.1:8080/
- Supervisor status: http://127.0.0.1:8090/status

Confirm both daemons before the first task:

```bash
curl -s http://127.0.0.1:7701/health
curl -s http://127.0.0.1:7702/health
```

Each response has `base_ready: true` and a `boot_id`. The hosts panel shows both cards as healthy, generation `g1`.

## A normal run

1. Under **Samples**, click **Dummy repo task**. That fills the prompt and loads the whole dummy repo (README, `inventory/`, and `tests/`). **New codebase** is the other sample: it clears the workspace and fills a prompt to create the splitbill package. Pick one sample at a time; each one sets the prompt and the workspace together.
2. Click **Start**. The new session appears on the left. The host that claimed it is outlined on the right, with the session title on that card.
3. Click **Live code** to read the workspace files. Edited files update as `write_file` and `replace_in_file` succeed.
4. Click **Trajectory**, or open `http://127.0.0.1:8080/sessions/<session_id>/trajectory` in its own tab. Each model step shows the clock time, duration, context sent to the model, the model response, and every tool call with its request, response, host, attempt, and any failure text. Tokens for the open step appear while they stream. The address in that tab is the link for this session's trajectory.

The right-hand timeline is the event log. Tool rows are named (`read_file · succeeded`). Click one for the request and the response.

## What to watch during a failure

| You did | Where it shows |
| --- | --- |
| Killed a host | The card goes `suspect` and the session stays. After 3 missed heartbeats it goes `down` and the session moves. The replacement container does not start until 20 seconds after the kill, then the card shows `replacing` and healthy at `g2`. |
| Tool was in flight | The tool card shows `attempts 2`. A journal hit also shows `replayed`. The same `op_id` is retried. Completed calls are not repeated. |
| Killed the backend | The connection pill goes `reconnecting (n)`, then `resumed`. The top bar restart count increases. A **session resumed** banner appears within about 10 seconds. |
| No other host is healthy | The session stays on the killed host and resumes there when that container is healthy again. |

Host restart lines are not repeated in the session column. Use the host cards and the timeline (`host.down`, `host.replacing`, `host.replaced`).

Do not press Ctrl+C in the `start.sh` terminal during these demos. That stops the supervisor, so nothing restarts the backend. **Kill backend** only kills the child process.

## Host death

Start a task and wait until tool cards are appearing. On the host card that lists this session, click **Kill host**.

The same action from a second terminal:

```bash
./scripts/kill_host.sh a
```

Use `b` if the session is on host-b.

What happens:

1. The card goes `suspect`. The session stays on that host. Heartbeats are every 2 seconds, and the session does not move until 3 of them have failed, about 6 seconds.
2. The banner says **migrated host-a → host-b**, and the session title leaves the killed card. The replacement container is not started until 20 seconds after the kill.
3. The card then shows `replacing`, then healthy with generation `g2` and a new short `boot_id`. It no longer owns the session.
4. The in-flight tool runs on the other host. The card shows a higher attempt count. If the first attempt had already been journaled, the badge says `replayed` and the file is not written twice. **Live code** still shows the same workspace.

`docker compose` is set to `restart: "no"`, so the killed container stays dead until the backend starts a new one.

## Backend death

Wait until a tool call is running. Click **Kill backend**.

The same action from a second terminal:

```bash
curl -s -X POST http://127.0.0.1:8090/kill-backend
curl -s http://127.0.0.1:8090/status
```

`restart_count` goes up by one. The new child pid appears within about a second. The page reconnects on its own.

What happens:

1. The pill shows `reconnecting`, then `resumed`.
2. The lease lasts 10 seconds and is renewed every 3 seconds, so the new process claims the session within about 10 seconds.
3. A **session resumed** banner appears. Open **Trajectory** and the earlier steps are still there, including finished tool requests and responses.
4. A tool that had already succeeded does not run again. A tool that was still `pending` or `running` runs with the same `op_id`.

## Host and backend together

Wait until tools are in progress. Note which host owns the session.

1. Click **Kill host** on that host.
2. Immediately click **Kill backend**.

Or:

```bash
./scripts/kill_host.sh a
curl -s -X POST http://127.0.0.1:8090/kill-backend
```

The supervisor brings the backend back. The new backend keeps the session on the dead host until 3 heartbeats miss, then moves it to the other healthy host and replaces the killed container. The run finishes on the same files. **Trajectory** shows one continuous step list, with the migration in the step that was in flight.

## Reload the page

Refresh the browser in the middle of a run, or after one of the kills. The session list, transcript, tool cards, changed files, and trajectory come back from SQLite. The event stream continues from the last id. Streaming tokens that had not been committed yet are gone. The saved model response for that step appears when the step commits.

## Tests for the same guarantees

These do not need Docker or a model key:

```bash
./scripts/test.sh tests/test_workspace.py tests/test_journal.py tests/test_store.py tests/test_agent.py tests/test_scheduler.py -q
```

They cover a crash mid-copy of the workspace, a duplicate `op_id` returning the same body, `replace_in_file` reporting `already_applied`, and a stale lease owner being refused.
