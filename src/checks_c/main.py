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

Toolchain: ubuntu:24.04 + build-essential/autoconf/libtool/libcurl/libjansson/
openssl/libgmp/libhwloc/ncurses/protobuf/qrencode/libdb++ (autotools builds
whose depends self-hosts rust/bdb/boost need no host rust), ccache volume for
incremental rebuilds (cache contract P3).

Artifact gate: `build.artifact: <filename-glob>` in truenorth.yaml replaces
the legacy cpuminer*/minerd* name gate (e.g. `artifact: verusd` finds an ELF
named verusd at maxdepth 2). Absent key = legacy name gate (back-compat).
Run: dagger call --mod github.com/snoby-demo-org/dagger-checks-c build --source=.
"""

import json
import re

import dagger
from dagger import dag, function, object_type

TOOLCHAIN_IMAGE = "ubuntu:24.04"
APT_PACKETS = (
    "build-essential autoconf automake pkg-config ccache "
    "libcurl4-openssl-dev libjansson-dev libssl-dev libgmp-dev "
    "libhwloc-dev zlib1g-dev patch "
    # autotools builds whose depends system self-hosts heavy deps (e.g. veruscoin:
    # depends downloads rust 1.69 + bdb + boost itself — NO host rustc/cargo needed):
    "libtool ncurses-dev unzip wget bsdmainutils python3 python-is-python3 "
    "libprotobuf-dev protobuf-compiler libqrencode-dev libdb++-dev "
    "git curl ca-certificates"
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
    if [ -n "${TN_ARTIFACT:-}" ]; then
      log "artifact gate: pattern=$TN_ARTIFACT (build.artifact)"
      for c in $(find /src -maxdepth 2 -type f -perm -u+x -name "$TN_ARTIFACT" 2>/dev/null); do
        if [ "$(head -c 4 "$c" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then BIN="$c"; break; fi
      done
    else
      for c in $(find /src -maxdepth 2 -type f -perm -u+x \( -name 'cpuminer*' -o -name 'minerd*' \) 2>/dev/null); do
        if [ "$(head -c 4 "$c" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then BIN="$c"; break; fi
      done
    fi
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


def _parse_artifact(text: str) -> str:
    """Extract build.artifact from truenorth.yaml — a filename GLOB for the
    artifact gate (e.g. `artifact: verusd`), replacing the legacy
    cpuminer*/minerd* name gate when present. Sits directly under `build:`
    (sibling of hooks:/matrix:); per-variant artifacts are out of scope.
    Empty string when absent (legacy name gate applies)."""
    top = None
    seen_build = False
    seen_matrix = False
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            top = key0.strip()
            seen_build = top == "build" and not val0.strip()
            seen_matrix = False
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
            seen_matrix = False
            continue
        if key == "matrix" and not val and seen_build:
            seen_matrix = True
            continue
        if seen_build and not seen_matrix and key == "artifact" and val:
            return _strip_outer_quotes(val)
    return ""


def _parse_build_key(text: str, key: str) -> str:
    """Extract a scalar key directly under the build block (e.g.
    `depends_image: registry.viporlab.net/demo/verus-depends:d-abc123`).
    Empty string when absent."""
    top = None
    seen_build = False
    seen_matrix = False
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            top = stripped.partition(":")[0].strip()
            seen_build = top == "build" and not stripped.partition(":")[2].strip()
            seen_matrix = False
            continue
        if ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        k = k.strip()
        v = v.strip()
        if k == "build" and not v:
            seen_build = True
            seen_matrix = False
            continue
        if k == "matrix" and not v and seen_build:
            seen_matrix = True
            continue
        if seen_build and not seen_matrix and k == key and v:
            return _strip_outer_quotes(v)
    return ""


def _parse_publish(text: str) -> str:
    """Extract build.publish from truenorth.yaml — the STAGING REPO REF to
    publish the runtime image to after the artifact gate passes (e.g.
    `publish: registry.viporlab.net/demo/verus-staging`). Empty string when
    absent -> build stays compile+gate only (heavy builds never run on PRs,
    but an undeclared key can never publish by construction).
    """
    top = None
    seen_build = False
    seen_matrix = False
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            top = key0.strip()
            seen_build = top == "build" and not val0.strip()
            seen_matrix = False
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
            seen_matrix = False
            continue
        if key == "matrix" and not val and seen_build:
            seen_matrix = True
            continue
        if seen_build and not seen_matrix and key == "publish" and val:
            return _strip_outer_quotes(val)
    return ""


def _parse_scan(text: str) -> dict:
    """Extract build.scan from truenorth.yaml — the image-vulnerability gate
    config (v1.6.0). Runs Syft (SBOM) + Grype (CVE scan) on the gated image
    BETWEEN the artifact gate and the staging publish, so a fail means no
    staging tag is ever pushed (and therefore nothing reaches the Kargo
    warehouse -> promotion chain).

    Shape:
      build:
        scan:
          enabled: true
          fail_on: high,critical     # severities that fail the build
          ignore_unfixed: true       # only fixable CVEs count (noise floor)
          sbom_repo: registry.viporlab.net/demo/verus-sbom

    Returns {} when absent/disabled -> zero behavior change for existing
    pipelines (undeclared key can never gate by construction).
    """
    top = None
    seen_build = False
    seen_matrix = False
    seen_scan = False
    out: dict = {}
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            top = key0.strip()
            seen_build = top == "build" and not val0.strip()
            seen_matrix = False
            seen_scan = False
            continue
        if top not in ("checks", "build") or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "build" and not val:
            seen_build = True
            seen_matrix = False
            seen_scan = False
            continue
        if key == "matrix" and not val and seen_build:
            seen_matrix = True
            seen_scan = False
            continue
        if seen_build and not seen_matrix and key == "scan" and not val:
            seen_scan = True
            continue
        if seen_scan:
            if indent <= 2:  # left the scan block
                seen_scan = False
                continue
            if key == "enabled":
                out["enabled"] = val.lower() == "true"
            elif key == "fail_on" and val:
                out["fail_on"] = _strip_outer_quotes(val)
            elif key == "ignore_unfixed":
                out["ignore_unfixed"] = val.lower() == "true"
            elif key == "sbom_repo" and val:
                out["sbom_repo"] = _strip_outer_quotes(val)
    return out


# lint config lives at TOP LEVEL of truenorth.yaml (keys under checks: are
# fanned out as jobs by the harness, so checks.lint-config would dispatch a
# phantom `dagger call lint-config`). Accepted forms: top-level lint-config:
# block, or a `lint:` block form under checks: (config keys alongside
# enabled/mode/module). Not accepted: checks.lint-config sibling (v1.8.0
# mistake — run 42373718960 fanned it out and failed on unknown command).
def _parse_lint(text: str) -> dict:
    """Extract the checks.lint config block (v1.8.0 portability).

    Shape (all keys optional, no block = fully generic defaults):
      lint:
        std: c++17            # passed as --std=<std> (cppcheck accepts one)
        exclude:              # dirs relative to repo root, NOT linted
          - depends
          - src/secp256k1

    Nothing project-specific lives in the module: a new C project wires in
    with zero module changes; it declares its own layout here.
    """
    out: dict = {"std": [], "exclude": []}
    lint_indent = 2
    top = None
    seen_checks = False
    seen_lint = False
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        stripped = ln.strip()
        if indent == 0:
            key0, _, val0 = stripped.partition(":")
            if key0.strip() == "lint-config" and not val0.split("#")[0].strip():
                seen_lint = True
                lint_indent = indent
                continue
            top = key0.strip()
            seen_checks = top == "checks"
            seen_lint = False
            continue
        if not seen_checks and not seen_lint:
            continue
        if not seen_lint:
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            # accept either `lint:` block form or a sibling `lint-config:` block
            if key.strip() in ("lint", "lint-config") and not val.split("#")[0].strip():
                seen_lint = True
                lint_indent = indent
            continue
        # inside the lint block
        if indent <= lint_indent:  # left the lint block
            seen_lint = False
            continue
        if stripped.startswith("- "):
            item = _strip_outer_quotes(stripped[2:].split("#")[0].strip())
            if item:
                out["exclude"].append(item)
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "std" and val:
            body = val.split("#")[0].strip()
            if body.startswith("[") or "," in body:
                out["std"] = [_strip_outer_quotes(v.strip())
                              for v in body.strip("[]").split(",") if v.strip()]
            else:
                out["std"] = [_strip_outer_quotes(body)]
        elif key == "exclude" and val.split("#")[0].strip():
            out["exclude"] = [_strip_outer_quotes(v.strip())
                              for v in val.split("#")[0].strip("[]").split(",") if v.strip()]
    # These values are interpolated into a `sh -c` cppcheck command line —
    # fail LOUDLY on anything that isn't a plain path/standard token rather
    # than risking command injection from a repo-controlled yaml.
    import string as _string
    _safe = set(_string.ascii_letters + _string.digits + "+._-/[]")
    for s in out["std"]:
        if not s or set(s) - _safe:
            raise RuntimeError(f"lint-config std has unsafe characters: {s!r}")
    for e in out["exclude"]:
        if not e or set(e) - _safe:
            raise RuntimeError(f"lint-config exclude has unsafe characters: {e!r}")
    return out


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


async def _stage_commit_sha(source: dagger.Directory) -> str:
    """Resolve the source tree's commit. Dagger directory sync EXCLUDES .git,
    so in-container `git rev-parse` always fails on CI checkouts. The machinery
    writes the commit to .truenorth_commit before `dagger call`; read that
    first, fall back to git for local fixture use."""
    try:
        return (
            await source.file(".truenorth_commit").contents()
        ).strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        return (
            await dag.container()
            .from_(TOOLCHAIN_IMAGE)
            .with_mounted_directory("/src", source)
            .with_workdir("/src")
            .with_exec(["git", "rev-parse", "HEAD"])
            .stdout()
        ).strip()
    except Exception:  # noqa: BLE001 — no .git (local fixture) -> sha-less tag
        return ""


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
        try:
            cfg = await source.file(".github/truenorth.yaml").contents()
            hooks = _parse_hooks(cfg)
            matrix = _parse_matrix(cfg)
            artifact = _parse_artifact(cfg)
            publish_repo = _parse_publish(cfg)
            depends_image = _parse_build_key(cfg, "depends_image")
            # v1.8.0 portability knobs — no project paths in the module:
            #   dockerfile:    path to the Dockerfile for the docker-build path
            #   artifact_path: binary path INSIDE the built image (gate probe)
            # Defaults keep back-compat: veruscoin's layout verbatim.
            dockerfile = _parse_build_key(cfg, "dockerfile") or "docker/Dockerfile.release"
            artifact_path = _parse_build_key(cfg, "artifact_path") or (
                f"/usr/local/bin/{artifact}" if artifact else "/usr/local/bin/artifact")
            scan_cfg = _parse_scan(cfg)
        except Exception:  # noqa: BLE001 — no/unreadable truenorth.yaml -> legacy path
            hooks, matrix, artifact, publish_repo, depends_image = {}, {}, "", "", ""
            scan_cfg = {}
            dockerfile, artifact_path = "docker/Dockerfile.release", "/usr/local/bin/artifact"
        venv: dict[str, str] = {}
        if variant:
            if variant not in matrix:
                raise RuntimeError(
                    f"unknown build variant '{variant}' — declared in "
                    f"truenorth.yaml: {sorted(matrix) or 'NONE (no matrix block)'}")
            entry = matrix[variant]
            venv = {k: v for k, v in (entry.get("env") or {}).items()}
            hooks = {**hooks, **{k: v for k, v in (entry.get("hooks") or {}).items() if v}}

        # ---- PATH A (v1.4.0): docker/Dockerfile.release present -> the build
        # IS a docker build (the proven buildx recipe), inside the engine
        # (BuildKit cache; depends-baker/app-builder layers stay cached across
        # runs). The gate extracts the binaries FROM the built image; publish
        # pushes that SAME image by digest. Build once, promote everywhere.
        dockerfile_present = True
        try:
            await source.file(dockerfile).size()
        except Exception:  # noqa: BLE001
            dockerfile_present = False

        sha = await _stage_commit_sha(source)
        img = None
        if dockerfile_present:
            bargs = [dagger.BuildArg(name="GIT_SHA", value=sha or "unknown")]
            if depends_image:
                bargs.append(dagger.BuildArg(name="DEPENDS_IMAGE", value=depends_image))
            build_kwargs = {
                "dockerfile": dockerfile,
                "build_args": bargs,
                "secrets": [],
            }
            try:
                img = await source.docker_build(**build_kwargs)
            except Exception as e:  # noqa: BLE001 — BuildKit failure text carries the tail
                tail = "\n".join(str(e).splitlines()[-60:])
                raise RuntimeError(f"build FAILED at stage=docker-build\n{tail}") from e
            # GATE: binaries from the built image — ELF magic + version.
            # Use an exec-based magic probe: the artifact exceeds Dagger's 128 MiB
            # file-contents limit, so .contents() on the binary fails outright.
            magic = (
                await img.with_entrypoint([])
                .with_exec(["sh", "-c",
                            f"head -c 4 {artifact_path}"
                            " | od -An -tx1 | tr -d ' \\n'"])
                .stdout()
            ).strip()
            if magic != "7f454c46":
                raise RuntimeError(
                    f"build FAILED at stage=artifact-gate: {artifact_path} "
                    f"in built image is not an ELF binary (magic={magic!r})")
            try:
                version = await (
                    img.with_entrypoint([])
                    .with_exec(["sh", "-c",
                                f"{artifact_path} --version 2>&1 | head -1"])
                    .stdout()
                )
                version = version.strip()
            except Exception as e:  # noqa: BLE001
                version = ""
                lines = [f"  WARN: {artifact_path} --version probe failed: {e}"]
            status = {"status": "ok", "failed_stage": None, "warnings": 0}
            binary = version
            payload = {
                "check": "build",
                "tool": f"docker-build@{dockerfile}",
                "status": "pass",
                "failed_stage": None,
                "warnings": 0,
                "binary": version or None,
            }
            if variant:
                payload["variant"] = variant
            lines = ["=" * 50, f" [build] {dockerfile} (buildx recipe, in-engine)"]
            if version:
                lines.append(f"  version: {version[:100]}")
        else:
        # ---- PATH B (legacy): native autotools hook chain
            if jobs:
                script = BUILD_SCRIPT.replace('make -j"$(nproc)"', f"make -j{jobs}")
            else:
                script = BUILD_SCRIPT
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
                .with_env_variable("TN_ARTIFACT", artifact)
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

        # ---- IMAGE SCAN (v1.6.0): Syft SBOM + Grype CVE gate on the gated
        # image, BEFORE the staging publish. A fail here means no staging tag
        # is pushed at all — the Kargo warehouse/promotion chain never sees
        # the artifact (promotion-not-rebuild: the scanned digest IS the
        # the digest that gets promoted). Image is scanned via as_tarball (no
        # registry round-trip). Grype DB rides a persistent cache volume so
        # the ~100MB download is paid once per engine, not per run.
        if scan_cfg.get("enabled") and status["status"] == "ok" and img is not None:
            fail_on = [s.strip().lower() for s in str(scan_cfg.get("fail_on", "high,critical")).split(",") if s.strip()]
            ignore_unfixed = bool(scan_cfg.get("ignore_unfixed", True))
            sbom_repo = str(scan_cfg.get("sbom_repo", "")).rstrip("/")
            lines.append("=" * 50)
            lines.append(f" [build] image scan (syft+grype, fail_on={','.join(fail_on)}"
                         f"{' , ignore-unfixed' if ignore_unfixed else ''})")
            try:
                tar_f = img.as_tarball()
                # anchore/syft is shell-less too (v1.6.1): wrap the binary in
                # alpine so sh -c can mkdir -p /out (syft's --file won't).
                syft_ctr = (
                    dag.container()
                    .from_("alpine:3.20")
                    .with_file("/bin/syft", dag.container().from_("anchore/syft:latest").file("/syft"))
                    .with_mounted_file("/scan/scan.img", tar_f)
                    .with_exec(["sh", "-c",
                                "mkdir -p /out && syft /scan/scan.img -o cyclonedx-json --file /out/sbom.json"])
                )
                sbom_f = syft_ctr.file("/out/sbom.json")
                # --fail-on takes ONE severity, not a list: pass the highest in
                # fail_on and decide the full verdict from the parsed counts.
                _sev_order = {"negligible": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
                top_sev = max(fail_on, key=lambda s: _sev_order.get(s, 9))
                # anchore/grype is shell-less; wrap the binary in alpine so the
                # DB cache dir mounts writable and diagnostics are capturable.
                grype_ctr = (
                    dag.container()
                    .from_("alpine:3.20")
                    .with_file("/bin/grype", dag.container().from_("anchore/grype:latest").file("/grype"))
                    .with_mounted_file("/scan/scan.img", tar_f)
                    .with_mounted_cache("/grype-db", dag.cache_volume("grype-db"))
                    .with_env_variable("GRYPE_DB_CACHE_DIR", "/grype-db")
                    .with_exec(["sh", "-c",
                                "mkdir -p /out && grype /scan/scan.img -o json --file /out/grype.json "
                                f"--fail-on {top_sev} {'--only-fixed' if ignore_unfixed else ''}"],
                               expect=dagger.ReturnType.ANY)
                )
                # grype exits non-zero when fail-on severities are present:
                # read the report either way, then decide on the counts.
                report_raw = await grype_ctr.file("/out/grype.json").contents()
                report = json.loads(report_raw)
                matches = report.get("matches", [])
                counts: dict[str, int] = {}
                for m in matches:
                    sev = ((m.get("vulnerability") or {}).get("severity") or "unknown").lower()
                    counts[sev] = counts.get(sev, 0) + 1
                failing = {s: counts.get(s, 0) for s in fail_on if counts.get(s)}
                total = len(matches)
                lines.append(f"  vulnerabilities: {total} matched"
                             + (f"; failing: {failing}" if failing else "; none at fail-on severity"))
                sbom_addr = ""
                if sbom_repo:
                    sbom_addr = f"{sbom_repo}:sha-{sha or 'unknown'}"
                    await (
                        dag.container()
                        .from_("alpine:3.20")
                        .with_file("/sbom.json", sbom_f)
                        .with_label("org.opencontainers.image.title",
                                    sbom_repo.rsplit("/", 1)[-1])
                        .publish(sbom_addr)
                    )
                    lines.append(f"  sbom: {sbom_addr}")
                    payload["sbom"] = sbom_addr
                payload["scan"] = {"status": "pass" if not failing else "fail",
                                   "counts": counts, "total": total}
                if failing:
                    top = sorted(matches, key=lambda m: -len(json.dumps(m)))[:10]
                    summary = "\n".join(
                        f"    {((m.get('vulnerability') or {}).get('id') or '?')} "
                        f"[{((m.get('vulnerability') or {}).get('severity') or '?')}] "
                        f"{((m.get('artifact') or {}).get('name') or '?')} "
                        f"{(m.get('artifact') or {}).get('version') or ''} -> "
                        f"{((m.get('vulnerability') or {}).get('fix', {}) or {}).get('versions') or 'no fix'}"
                        for m in top)
                    raise RuntimeError(
                        f"build FAILED at stage=image-scan: vulnerabilities at "
                        f"fail-on severity ({', '.join(f'{k}={v}' for k, v in failing.items())})\n"
                        f"  no staging tag was pushed; fix or bump the base image, or\n"
                        f"  adjust build.scan (fail_on / ignore_unfixed) in truenorth.yaml\n"
                        f"  top findings:\n{summary}")
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001 — scan infrastructure failure is a hard fail
                raise RuntimeError(
                    f"build FAILED at stage=image-scan (infrastructure): {e}\n"
                    f"  the gate cannot prove the image clean — no staging tag pushed (FR-59)") from e

        # ---- PUBLISH (v1.4.0): push the gated image itself by digest
        if publish_repo and status["status"] == "ok" and img is not None:
            tag = "sha-" + (sha or "unknown") + (f"-{variant}" if variant else "")
            addr = f"{publish_repo}:{tag}"
            lines.append(f"  staging: publishing gated image -> {addr}")
            digest = await img.publish(addr)
            payload["digest"] = digest
            payload["staging"] = addr
            lines.append(f"  staged: {addr}@{digest}")

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

    @function
    async def lint(self, source: dagger.Directory) -> str:
        """REAL — cppcheck static analysis over the C/C++ source (fast tier).

        Runs cppcheck (--enable=warning,performance) over the whole source
        tree, counts findings by severity, emits the FR-7 TRUENORTH_RESULT
        contract line, and raises when anything at severity 'error' exists
        (style/performance noise is reported, not failed — the truenorth.yaml
        mode decides blocking: advisory first per FR-12, flip to blocking
        after the error budget has a baseline).

        Wiring (v1.7.0): point the lint slot's module at this repo and enable
        it — the machinery dispatches `dagger call --mod <module> lint` generically.
        Layout knobs (v1.8.0) come from checks.lint in truenorth.yaml:
        std (compiler std passed to --std) and exclude (repo-relative dirs to
        skip — vendored/third-party code). Defaults: no --std flag, no excludes.
        Performance (v1.8.3): apk toolchain is its own cached exec; cppcheck
        runs with --cppcheck-build-dir=/cc on the tn-cppcheck-build cache
        volume (incremental — repeat runs re-scan only changed files) and
        bounded -j8 (was -j$(nproc)).
        """
        try:
            lint_cfg = _parse_lint(await source.file(".github/truenorth.yaml").contents())
        except Exception:  # noqa: BLE001 — no/unreadable truenorth.yaml -> generic defaults
            lint_cfg = {"std": [], "exclude": []}
        report = "/tmp/cppcheck.xml"
        std_flags = " ".join(f"--std={s}" for s in lint_cfg.get("std") or [])
        std_flags = std_flags + " " if std_flags else ""
        ignore_flags = " ".join(f"-i/src/{d.strip('/')}" for d in lint_cfg.get("exclude", []))
        ctr = (
            dag.container()
            .from_("alpine:3.20")
            # v1.8.3: toolchain install is its OWN exec so the apk layer caches
            # across commits (alpine digest unchanged -> hit; the scan exec
            # re-runs alone on source changes).
            .with_exec(["sh", "-c", "apk add --no-cache cppcheck >/dev/null 2>&1"])
            # v1.8.3: incremental-analysis state on an engine-local cache
            # volume — repeat runs re-scan only changed files (measured 147x
            # on a 164-file subset: 886s cold -> 6s warm). Engine-local by
            # design; works with the node-local engine routing.
            .with_mounted_cache("/cc", dag.cache_volume("tn-cppcheck-build"))
            .with_mounted_directory("/src", source)
            .with_workdir("/src")
            .with_exec(["sh", "-c",
                        "cppcheck /src --enable=warning,performance "
                        f"--inline-suppr {std_flags}--cppcheck-build-dir=/cc "
                        # v1.8.3: bounded parallelism (was -j"$(nproc)"). Cold
                        # duplicate-eval storms ran 2+ identical -j16 trees on a
                        # shared 16-CPU node (load 48.65); -j8 halves the
                        # worst-case contention.
                        "-j8 "
                        f"{ignore_flags} "
                        f"--error-exitcode=0 --xml --xml-version=2 "
                        f"--output-file={report} >/dev/null 2>&1; "
                        "grep -c '<error' " + report + " || true; "
                        # v1.8.4: surface per-finding detail (id/file/line) —
                        # the XML went only to --output-file before, so job
                        # logs showed counts but never WHICH findings.
                        "grep -oE '<error[^>]{0,160}' " + report + " | head -30; "
                        "grep -oE 'severity=\"[a-z]+\"' " + report + " | sort | uniq -c"])
        )
        console = await ctr.stdout()
        counts: dict[str, int] = {}
        for ln in console.splitlines():
            m = re.match(r"\s*(\d+)\s*severity=\"(\w+)\"", ln)
            if m:
                counts[m.group(2)] = int(m.group(1))
        total = sum(counts.values())
        errors = counts.get("error", 0)
        payload = {
            "check": "lint",
            "tool": "cppcheck@alpine3.20",
            "total": total,
            "counts": counts,
            "status": "fail" if errors else "pass",
        }
        lines = [
            "=" * 50,
            " [lint] cppcheck (warning,performance)",
            f"  findings: {total} {counts}",
        ]
        for ln in console.splitlines():
            if "<error" in ln or "<warning" in ln:
                lines.append("  " + ln.strip()[:160])
        lines = lines[:40]
        lines.append(f"TRUENORTH_RESULT: {json.dumps(payload)}")
        lines.append("=" * 50)
        if errors:
            raise RuntimeError(f"cppcheck: {errors} finding(s) at severity=error (see job log)")
        return "\n".join(lines)
