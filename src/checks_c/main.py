"""C/C++ build checks for viporlab repos (Dagger module).

Language-specific module per the TrueNorth per-language-module decision:
patch -> configure -> make, entirely inside a pinned toolchain container
(no runner labels, no host toolchain — FR-73/FR-7/NFR-4).

Build contract — repo-side control files (presence-detected, no config;
canonical doc: goldenpath TechDocs, "TrueNorth build contract"). Actual
stage flow: patch -> configure-or-build.sh -> make; the make stage ALWAYS
runs last in both paths:

    1. patch      patches/*.patch applied with `patch -p1` in glob order
                  -> status patch_failed, stage patch
    2. WITH build.sh:    autogen.sh (if present) -> ./build.sh
                         -> autogen_failed / build_failed, stage build.sh
       WITHOUT build.sh: autogen.sh (if present) -> ./configure
                         -> autogen_failed / configure_failed, stage
                         configure; no_configure when neither autogen.sh
                         nor an executable ./configure exists
    3. make       make -j"$(nproc)" (or -j<jobs> via the jobs param) runs
                  last in BOTH paths -> make_failed, stage make

DECLARED HOOKS (SHIPPED v1.1.1 — the preferred control surface): declare
explicit command lines in the repo's own .github/truenorth.yaml:

    build:
      module: github.com/snoby-demo-org/dagger-checks-c@v1.1.1
      hooks:
        autogen:   ./autogen.sh                  # optional
        configure: ./configure --with-curl       # optional
        make:      make clean && make -j"$(nproc)"  # optional

The module self-reads hooks from the mounted source (no workflow changes;
values are single-line commands, no inline comments, run via bash -c in
/src). DECLARED MODE: only the declared stages run, in autogen->configure->
make order — build.sh and generic make are IGNORED; anything left undeclared
is skipped (a missing make shows up as no_binary). Legacy presence path is
unchanged and still applies when no hooks are declared (incl. the known
wart: generic make runs after build.sh).

Known wart — full-takeover semantics (skip generic make when build.sh
exists) is a proposed v1.1.1 change, pending decision.

The check FAILS on any stage failure or when no ELF binary (cpuminer*/
minerd*, executable, maxdepth 2, magic 7f454c46) is found -> status
no_binary. Warnings are counted from make.log and reported; they do not
fail. On failure the module raises RuntimeError with the failing stage's
last 60 log lines; the TRUENORTH_RESULT line is emitted on PASS only.

Result contract (FR-7), emitted as "TRUENORTH_RESULT: {json}":
  {"check":"build","tool":"autotools@ubuntu:24.04","status":"pass|fail",
   "failed_stage":null|"patch"|"configure"|"build.sh"|"make",
   "warnings":N,"binary":"<first line of --version output>"}
Internal status strings (patch_failed etc.) appear only in console
[build] lines, not in the payload.

Matrix (v1.1.2 — SHIPPED, workflow fan-out): the reusable workflow
(truenorth-v2.1+) parses checks.build.matrix (mapping form, {id: {env, hooks}})
and fans ONE runner pod per id, calling build(--variant=<id>) in parallel.
The variant's env is exported before the hook chain; its hooks override the
base hooks per slot; the payload gains "variant": "<id>". No matrix: block
= single implicit variant (no --variant flag, payload unchanged). Unknown
variant -> hard error naming the declared ids. The check fails if ANY
variant job fails (workflow-level aggregation, truenorth_report.py).

Toolchain: ubuntu:24.04 + build-essential/autoconf/libcurl/libjansson/openssl/
libgmp/libhwloc, ccache volume for incremental rebuilds (cache contract P3).
Run: dagger call --mod github.com/snoby-demo-org/dagger-checks-c build --source=.
"""

import json

import dagger
from dagger import dag, function, object_type

TOOLCHAIN_IMAGE = "ubuntu:24.04"
APT_PACKETS = (
    "build-essential autoconf automake pkg-config ccache "
    "libcurl4-openssl-dev libjansson-dev libssl-dev libgmp-dev "
    "libhwloc-dev zlib1g-dev patch"
)

BUILD_SCRIPT = r"""
set -u
cd /src
STATUS=ok
STAGE=patch
log() { echo "[build] $*"; }

{
  log "stage=patch"
  if ls patches/*.patch >/dev/null 2>&1; then
    for p in patches/*.patch; do
      log "applying $p"
      patch -p1 < "$p" || { STATUS="patch_failed"; break; }
    done
  else
    log "no patches to apply"
  fi

  HOOKS_DECLARED=""
  if [ -n "${TN_HOOK_AUTOGEN:-}" ] || [ -n "${TN_HOOK_CONFIGURE:-}" ] || [ -n "${TN_HOOK_MAKE:-}" ]; then
    HOOKS_DECLARED=1
    log "declared hooks mode (build.sh and generic stages ignored)"
    if [ "$STATUS" = ok ] && [ -n "${TN_HOOK_AUTOGEN:-}" ]; then
      STAGE=autogen
      log "stage=autogen (declared)"
      bash -c "$TN_HOOK_AUTOGEN" > /tmp/configure.log 2>&1 || STATUS="autogen_failed"
    fi
    if [ "$STATUS" = ok ] && [ -n "${TN_HOOK_CONFIGURE:-}" ]; then
      STAGE=configure
      log "stage=configure (declared)"
      bash -c "$TN_HOOK_CONFIGURE" > /tmp/configure.log 2>&1 || STATUS="configure_failed"
    fi
    if [ "$STATUS" = ok ] && [ -n "${TN_HOOK_MAKE:-}" ]; then
      STAGE=make
      log "stage=make (declared)"
      bash -c "$TN_HOOK_MAKE" > /tmp/make.log 2>&1 || STATUS="make_failed"
    fi
  elif [ "$STATUS" = ok ] && [ -f ./build.sh ]; then
    STAGE=build.sh
    log "stage=build.sh (project's own build script)"
    if [ -f ./autogen.sh ]; then
      log "pre-stage: autogen (fresh clone has no regenerated autotools)"
      ./autogen.sh > /tmp/configure.log 2>&1 || STATUS="autogen_failed"
    fi
    if [ "$STATUS" = ok ]; then
      chmod +x ./build.sh
      ./build.sh > /tmp/make.log 2>&1 || STATUS="build_failed"
    fi
  elif [ "$STATUS" = ok ]; then
    STAGE=configure
    log "stage=configure"
    if [ -f ./autogen.sh ]; then
      # regenerate autotools artifacts (shipped configure can be stale vs Makefile.am)
      ./autogen.sh > /tmp/configure.log 2>&1 || STATUS="autogen_failed"
    elif [ ! -x ./configure ]; then
      STATUS="no_configure"
    fi
    if [ "$STATUS" = ok ]; then
      ./configure > /tmp/configure.log 2>&1 || STATUS="configure_failed"
    fi
  fi

  # generic make: legacy paths only — in declared mode the make hook IS the
  # make stage; anything not declared does not run
  if [ "$STATUS" = ok ] && [ -z "$HOOKS_DECLARED" ]; then
    STAGE=make
    log "stage=make ($(nproc) jobs)"
    make -j"$(nproc)" > /tmp/make.log 2>&1 || STATUS="make_failed"
  fi

  if [ "$STATUS" = ok ]; then
    BIN=""
    for c in $(find /src -maxdepth 2 -type f -perm -u+x \( -name 'cpuminer*' -o -name 'minerd*' \) 2>/dev/null); do
      if [ "$(head -c 4 "$c" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then BIN="$c"; break; fi
    done
    if [ -n "$BIN" ]; then
      log "binary: $BIN"
      "$BIN" --version > /tmp/binary.txt 2>&1 || true
    else
      STATUS="no_binary"
    fi
  fi

  WARN=$(grep -ci 'warning' /tmp/make.log 2>/dev/null); WARN=${WARN%%$'\n'*}; WARN=${WARN:-0}
  ERRS=$(grep -ci 'error' /tmp/make.log 2>/dev/null); ERRS=${ERRS%%$'\n'*}; ERRS=${ERRS:-0}
  printf '{"status":"%s","failed_stage":"%s","warnings":%s,"errors":%s}\n' \
    "$STATUS" "$STAGE" "$WARN" "$ERRS" > /tmp/cbuild_status.json
  log "status=$STATUS stage=$STAGE warnings=$WARN errors=$ERRS"
} > /tmp/cbuild_console.log 2>&1
exit 0
"""


_HOOK_KEYS = ("autogen", "configure", "make")


def _parse_hooks(text: str) -> dict[str, str]:
    """Extract build.hooks.{autogen,configure,make} from truenorth.yaml.

    Minimal line-based parse of our own template-controlled schema (the
    module runtime has no pyyaml): a top-level `build:` block, an indented
    `hooks:` mapping inside it, single-line `key: value` entries. Values are
    used verbatim (outer quotes stripped); unknown keys are ignored.
    """
    hooks: dict[str, str] = {}
    top = None          # current top-level key
    seen_build = False  # a build: key seen inside the current top-level block
    seen_matrix = False # matrix: seen -> base hooks block closed forever
                        # (variant hooks only exist UNDER matrix ids)
    in_hooks = False
    hooks_indent = -1
    leaf_indent = -1    # indent of the first captured leaf; later leaves must
                        # match it exactly, so deeper-nested sections (a
                        # variant's hooks: leaves) can never be absorbed here
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            key0 = key0.strip()
            val0 = val0.strip()
            top = key0
            # a top-level build: block counts as "seen build" too
            seen_build = key0 == "build" and not val0
            seen_matrix = False
            in_hooks = False
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        # hooks may live under a top-level build: block or under checks: -> build:
        if top not in ("checks", "build"):
            continue
        if key == "build" and not val:
            seen_build = True
            seen_matrix = False
            in_hooks = False
            continue
        if key == "matrix" and not val and seen_build:
            seen_matrix = True
            in_hooks = False
            continue
        if key == "hooks" and not val and seen_build and not seen_matrix and not in_hooks:
            in_hooks = True
            hooks_indent = indent
            leaf_indent = -1
            continue
        if in_hooks:
            # hook leaves: same indent as the first leaf, non-empty value.
            # Anything else — dedent to a sibling section, an empty-val header
            # (a nested matrix:/hooks:), an indent jump — closes the block.
            if (val and indent > hooks_indent
                    and (leaf_indent == -1 or indent == leaf_indent)):
                if key in _HOOK_KEYS:
                    hooks[key] = _strip_outer_quotes(val)
                if leaf_indent == -1:
                    leaf_indent = indent
                continue
            in_hooks = False
    return hooks


def _strip_outer_quotes(val: str) -> str:
    """Strip a fully-quoted wrapper only ("..." / '...') — never naive
    both-ends stripping (make -j"$(nproc)")."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


def _split_flow(s: str) -> list[str]:
    """Split a single-line YAML flow mapping body on top-level commas
    (quote-aware, escape-aware): 'MARCH: znver2, FOO: "a, b"' ->
    ['MARCH: znver2', 'FOO: "a, b"']. Handles backslash escapes inside
    double quotes and YAML doubled single quotes ('' -> ')."""
    parts: list[str] = []
    buf: list[str] = []
    q = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if q:
            if q == '"' and ch == "\\" and i + 1 < n:
                nxt = s[i + 1]
                if nxt in ('"', "\\"):
                    buf.append(nxt)      # YAML unescape: \" -> " and \\ -> \
                else:
                    buf.append(ch)       # unknown escape: keep verbatim
                    buf.append(nxt)
                i += 2
                continue
            if q == "'" and ch == "'" and i + 1 < n and s[i + 1] == "'":
                buf.append("'")          # YAML doubled single quote -> literal '
                i += 2
                continue
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _parse_matrix(text: str) -> dict[str, dict]:
    """Extract checks.build.matrix as {variant_id: {"env": {...}, "hooks": {...}}}.

    Mapping form (template-controlled schema, same minimal line-based parse
    as _parse_hooks — the module runtime has no pyyaml):

        checks:
          build:
            matrix:
              amd-znver2:
                env:
                  MARCH: znver2
                hooks:                      # optional per-variant hook override
                  configure: ./configure --foo
              intel-x86-64v2:
                env:
                  MARCH: x86-64-v2

    env values and hook lines are verbatim (outer quotes stripped); unknown
    sub-keys are ignored. env/hooks accept block form or single-line flow
    mappings (env: { MARCH: znver2 }). The reusable workflow (truenorth-v2.1)
    fans one runner pod per id and calls build(--variant=<id>).
    """
    entries: dict[str, dict] = {}
    top = None
    seen_build = False
    in_matrix = False
    matrix_indent = -1
    cur_id = None
    cur_sec = None       # "env" | "hooks" | None
    sec_indent = -1
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            top = key0.strip()
            seen_build = top == "build" and not val0.strip()
            in_matrix = False
            cur_id = None
            cur_sec = None
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if top not in ("checks", "build"):
            continue
        if key == "build" and not val:
            seen_build = True
            in_matrix = False
            cur_id = None
            cur_sec = None
            continue
        if key == "matrix" and not val and seen_build:
            in_matrix = True
            matrix_indent = indent
            cur_id = None
            cur_sec = None
            continue
        if not in_matrix:
            continue
        if indent <= matrix_indent:      # dedent: matrix block ended
            in_matrix = False
            cur_id = None
            cur_sec = None
            continue
        if cur_sec is not None and indent > sec_indent:
            # leaf of the current env/hooks section
            if val:
                entries[cur_id][cur_sec][key] = _strip_outer_quotes(val)
            continue
        if key in ("env", "hooks") and cur_id is not None:
            # section header (block form) or a single-line flow mapping:
            #   env: { MARCH: znver2, JOBS: "4" }
            if not val:
                cur_sec = key
                sec_indent = indent
            elif val.startswith("{") and val.endswith("}"):
                inner = val[1:-1].strip()
                if inner:
                    for part in _split_flow(inner):
                        k2, _, v2 = part.partition(":")
                        if v2.strip():
                            entries[cur_id][key][k2.strip()] = _strip_outer_quotes(v2.strip())
                cur_sec = None
            continue
        if not val:                       # a new variant id
            cur_id = key
            entries[cur_id] = {"env": {}, "hooks": {}}
            cur_sec = None
            sec_indent = indent
    return entries


def _toolchain() -> dagger.Container:
    return (
        dag.container()
        .from_(TOOLCHAIN_IMAGE)
        .with_mounted_cache("/var/cache/apt/archives", dag.cache_volume("apt-archives"))
        .with_exec(["bash", "-c", f"apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {APT_PACKETS}"])
        .with_env_variable("CC", "ccache gcc")
        .with_env_variable("CXX", "ccache g++")
        .with_mounted_cache("/cache/ccache", dag.cache_volume("ccache-c"))
        .with_env_variable("CCACHE_DIR", "/cache/ccache")
    )


@object_type
class ChecksC:
    @function
    async def build(self, source: dagger.Directory, jobs: int | None = None,
                    variant: str = "") -> str:
        """REAL — the C/C++ BUILD LEG (not a scan check): patch -> configure -> make (FR-73).

        Fails when patch/configure/make fails or no binary is produced.
        Warnings are counted and reported; they do not fail the check.

        Matrix variants (dagger-checks-c@v1.1.2): the reusable workflow fans
        one runner pod per id in checks.build.matrix and calls build with
        --variant=<id>. The variant's env is exported before the hook chain
        and its hooks: override the base hooks per slot; the result payload
        gains a "variant" field. Unknown variant -> hard error.
        """
        if jobs:
            script = BUILD_SCRIPT.replace('make -j"$(nproc)"', f"make -j{jobs}")
        else:
            script = BUILD_SCRIPT
        try:
            cfg = await source.file(".github/truenorth.yaml").contents()
            hooks = _parse_hooks(cfg)
            matrix = _parse_matrix(cfg)
        except Exception:  # noqa: BLE001 — no/unreadable truenorth.yaml -> legacy path
            hooks, matrix = {}, {}
        venv: dict[str, str] = {}
        if variant:
            if variant not in matrix:
                raise RuntimeError(
                    f"unknown build variant '{variant}' — declared in "
                    f"truenorth.yaml: {sorted(matrix) or 'NONE (no matrix block)'}")
            entry = matrix[variant]
            venv = {k: v for k, v in (entry.get("env") or {}).items()}
            # per-variant hook override wins per slot over the base hooks
            hooks = {**hooks, **{k: v for k, v in (entry.get("hooks") or {}).items() if v}}
        ctr = (
            _toolchain()
            .with_mounted_directory("/src", source)
            .with_workdir("/src")
        )
        for k in sorted(venv):
            ctr = ctr.with_env_variable(k, venv[k])
        ctr = (
            ctr
            .with_env_variable("TN_HOOK_AUTOGEN", hooks.get("autogen", ""))
            .with_env_variable("TN_HOOK_CONFIGURE", hooks.get("configure", ""))
            .with_env_variable("TN_HOOK_MAKE", hooks.get("make", ""))
            .with_new_file("/tmp/build.sh", script)
            .with_exec(["bash", "/tmp/build.sh"])
        )
        status_raw = await ctr.file("/tmp/cbuild_status.json").contents()
        console = await ctr.file("/tmp/cbuild_console.log").contents()
        status = json.loads(status_raw)
        binary = (await ctr.file("/tmp/binary.txt").contents()).strip() if status["status"] == "ok" else ""

        payload = {
            "check": "build",
            "tool": f"autotools@{TOOLCHAIN_IMAGE}",
            "status": "pass" if status["status"] == "ok" else "fail",
            "failed_stage": status.get("failed_stage") if status["status"] != "ok" else None,
            "warnings": status.get("warnings", 0),
            "binary": binary.splitlines()[0] if binary else None,
        }
        if variant:
            payload["variant"] = variant
        lines = ["=" * 50, " [build] patch -> configure -> make (real)"]
        lines.append(console.strip())
        if binary:
            lines.append(f"  version: {binary.splitlines()[0][:100]}")
        lines.append(f"TRUENORTH_RESULT: {json.dumps(payload)}")
        lines.append("=" * 50)
        if status["status"] != "ok":
            stage = status.get("failed_stage", "")
            log_name = "/tmp/make.log" if stage in ("make", "build.sh", "build_failed") else "/tmp/configure.log"
            try:
                log = await ctr.file(log_name).contents()
            except Exception:  # noqa: BLE001 — stage may not have produced its log
                log = console
            tail = "\n".join(log.splitlines()[-60:])
            raise RuntimeError(
                f"build FAILED at stage={status['failed_stage']}\n{tail}"
            )
        return "\n".join(lines)
