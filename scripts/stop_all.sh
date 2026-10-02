#!/usr/bin/env bash
# stop_all.sh — tear down EVERYTHING a SaaS-Bench run leaves behind.
#
#   bash scripts/stop_all.sh            # clean
#   bash scripts/stop_all.sh --dry-run  # show what would be cleaned, touch nothing
#
# Cleans, in order: the run orchestrator -> environment processes (chrome + Playwright MCP)
# -> rollout_* containers/volumes/networks -> transient tmp dirs. Agent CLIs (claude/kimi/
# codex) are reported but NEVER killed.
#
# SAFETY — this box is shared with other users who also run `claude`/`node`. Processes are
# therefore NEVER matched by bare binary name: a process must belong to the CURRENT user
# AND have its cwd (or argv) inside one of our tmp bases. On top of that, only the run
# orchestrator plus pure environment processes (chrome, Playwright MCP) are killed; agent
# CLIs are listed and left running, so an interactive coding session is never at risk.
#
# Env: SAAS_SLOT_PREFIX (default "rollout"), SAAS_BENCH_TMP (overrides the tmp base).

set -uo pipefail

PREFIX="${SAAS_SLOT_PREFIX:-rollout}"
TMPROOT="${TMPDIR:-/tmp}"
DRY=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]] && DRY=1

# Two conventions exist in-tree: slot.py/agent.py use saas_bench_<prefix> (compose files,
# chrome profiles, browser-use workdirs); harness/base.py uses saas_bench (CLI harness
# workdirs). Clean both, plus an explicit SAAS_BENCH_TMP if the run set one.
TMP_BASES=("${TMPROOT}/saas_bench_${PREFIX}" "${TMPROOT}/saas_bench")
[[ -n "${SAAS_BENCH_TMP:-}" ]] && TMP_BASES+=("${SAAS_BENCH_TMP}")

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if (( DRY )); then say "   [dry-run] $*"; else eval "$@" >/dev/null 2>&1 || true; fi; }

(( DRY )) && say "*** DRY RUN — nothing will be killed or removed ***"
say "prefix=${PREFIX}  tmp bases: ${TMP_BASES[*]}"

# ── 1. the orchestrator (kill first so it cannot spawn more work) ─────────────
# Restricted to python processes: a plain shell that merely mentions "saas_bench.run" in
# its argv (e.g. someone grepping for it) must not be killed.
orch_pids() {
    local p
    for p in $(pgrep -u "$(id -u)" -f 'saas_bench[.]run' 2>/dev/null || true); do
        [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
        case "$(ps -o comm= -p "$p" 2>/dev/null)" in python*) printf '%s\n' "$p";; esac
    done
}

step "run orchestrator (saas_bench.run)"
mapfile -t ORCH < <(orch_pids)
if ((${#ORCH[@]})); then
    say "   ${#ORCH[@]} process(es): ${ORCH[*]}"
    run "kill -TERM ${ORCH[*]}"
    (( DRY )) || sleep 3
    mapfile -t ORCH2 < <(orch_pids)
    ((${#ORCH2[@]})) && { say "   still alive, SIGKILL"; run "kill -9 ${ORCH2[*]}"; }
else
    say "   none"
fi

# ── 2. environment processes only: browsers + MCP servers ────────────────────
# Scoped the same way (current user AND cwd/argv under one of our tmp bases), but the
# agent CLIs themselves (claude / kimi / codex) are deliberately LEFT ALONE — killing
# anything named like an interactive coding session is too dangerous on a shared box.
# They are only reported; kill them yourself if a run really is wedged.
step "environment processes (chrome + Playwright MCP, tmp-scoped)"
declare -A VICTIMS=()
declare -A SKIPPED=()
SELF=$$
for pid in $(ps -u "$(id -u)" -o pid= 2>/dev/null); do
    [[ "$pid" == "$SELF" || "$pid" == "$PPID" ]] && continue
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || cmd=""
    ours=0
    for b in "${TMP_BASES[@]}"; do
        [[ "$cwd" == "$b"/* || "$cmd" == *"$b"/* ]] && { ours=1; break; }
    done
    (( ours )) || continue
    case "$(ps -o comm= -p "$pid" 2>/dev/null)" in
        chrome*|chromium*|headless_shell*|node|npm*|npx*)
            VICTIMS[$pid]=1 ;;
        *)  SKIPPED[$pid]=1 ;;   # claude / kimi / codex / anything else: hands off
    esac
done
if ((${#VICTIMS[@]})); then
    say "   ${#VICTIMS[@]} chrome/MCP process(es)"
    if (( DRY )); then
        for pid in "${!VICTIMS[@]}"; do
            say "   [dry-run] would kill $pid: $(ps -o comm= -p "$pid" 2>/dev/null)"
        done
    else
        kill -TERM "${!VICTIMS[@]}" 2>/dev/null || true
        sleep 3
        kill -9 "${!VICTIMS[@]}" 2>/dev/null || true
    fi
else
    say "   none"
fi
if ((${#SKIPPED[@]})); then
    say "   NOT killed (agent CLIs, by design): ${#SKIPPED[@]} process(es)"
    for pid in "${!SKIPPED[@]}"; do
        say "      $pid $(ps -o comm= -p "$pid" 2>/dev/null)"
    done
fi

# ── 3. containers / volumes / networks ───────────────────────────────────────
step "docker containers (${PREFIX}_*)"
mapfile -t CONTS < <(docker ps -a --format '{{.Names}}' 2>/dev/null | grep "^${PREFIX}_" || true)
if ((${#CONTS[@]})); then
    say "   ${#CONTS[@]} container(s)"
    run "docker rm -f -v ${CONTS[*]}"
else
    say "   none"
fi

step "docker volumes (${PREFIX}_*)"
mapfile -t VOLS < <(docker volume ls --format '{{.Name}}' 2>/dev/null | grep "^${PREFIX}_" || true)
if ((${#VOLS[@]})); then
    say "   ${#VOLS[@]} volume(s)"
    run "docker volume rm -f ${VOLS[*]}"
else
    say "   none"
fi

# compose apps (pretix/onlyoffice/...) create their own bridge networks
step "docker networks (${PREFIX}_*)"
mapfile -t NETS < <(docker network ls --format '{{.Name}}' 2>/dev/null | grep "^${PREFIX}_" || true)
if ((${#NETS[@]})); then
    say "   ${#NETS[@]} network(s)"
    run "docker network rm ${NETS[*]}"
else
    say "   none"
fi

# ── 4. transient files ───────────────────────────────────────────────────────
# Only the known generated names are removed, never a whole tmp base — a stray
# SAAS_BENCH_TMP pointing somewhere real must not turn this into rm -rf.
step "tmp dirs & compose files"
removed=0
for b in "${TMP_BASES[@]}"; do
    [[ -d "$b" ]] || continue
    while IFS= read -r -d '' p; do
        if (( DRY )); then say "   [dry-run] would remove $p"; else rm -rf -- "$p"; fi
        ((removed++))
    done < <(find "$b" -maxdepth 1 \
                \( -name 'harness_*' -o -name 'fs_*' -o -name 'chrome_*' \
                   -o -name "${PREFIX}_*.yml" \) -print0 2>/dev/null)
done
say "   ${removed} item(s)"

# ── 5. verdict ───────────────────────────────────────────────────────────────
step "after cleanup"
if (( DRY )); then
    say "   dry run — nothing changed"
else
    say "   orchestrator : $(orch_pids | wc -l)"
    say "   containers   : $(docker ps -aq --filter "name=^${PREFIX}_" 2>/dev/null | wc -l)"
    say "   volumes      : $(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -c "^${PREFIX}_" || true)"
fi
say ""
