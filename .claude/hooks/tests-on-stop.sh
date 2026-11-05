#!/usr/bin/env bash
# Stop hook: the agent may not finish a turn with failing tests after touching code.
# Exit 2 blocks the stop and hands stderr back to the agent; any other exit lets it stop.
cd "$CLAUDE_PROJECT_DIR" || exit 0

# second stop in a row (the agent already saw a failure): do not loop forever
python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("stop_hook_active") else 1)' && exit 0

[[ -z $(git status --porcelain -- app tests kb docker docker-compose.yml requirements.txt) ]] && exit 0
docker info > /dev/null 2>&1 || { echo "tests-on-stop: Docker unavailable, tests not run" >&2; exit 0; }

if ! out=$(docker compose run --rm test 2>&1); then
  echo "Tests fail after your changes. Fix them before finishing:" >&2
  tail -n 40 <<< "$out" >&2
  exit 2
fi
