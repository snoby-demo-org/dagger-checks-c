"""C/C++ build checks for viporlab repos (Dagger module).

Language-specific module per the TrueNorth per-language-module decision:
patch -> configure -> make, entirely inside a pinned toolchain container
(no runner labels, no host toolchain — FR-73/FR-7/NFR-4).

Build contract — the repo controls the build with files in its root
(presence-detected, no config; canonical doc: goldenpath TechDocs,
"TrueNorth build contract"):

    patch      patches/*.patch applied with `patch -p1` in glob order
               -> patch_failed
    configure  autogen.sh if present (fresh clone's shipped configure can be
               stale vs Makefile.am), else ./configure
               -> autogen_failed / configure_failed / no_configure
    build      build.sh if present — OWNS the whole build (its own
               configure+make; the generic path does not run); else generic
               make -j"$(nproc)" -> build_failed / make_failed

The check FAILS on any stage failure or when no ELF binary (cpuminer*/
minerd*) is produced; the failing stage's last 60 log lines are surfaced.
Warnings are counted and reported; they do not fail.

Result contract (FR-7): TRUENORTH_RESULT {"check":"build","tool":
"autotools@ubuntu:24.04","status","failed_stage","warnings","binary"}.

Matrix (contract documented in goldenpath, machinery pending): build
variants (e.g. -march per CPU target) fan out per-variant with env exported
before the hook chain; each result carries a "variant" field; the check
fails if ANY variant fails.

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

  if [ "$STATUS" = ok ] && [ -f ./build.sh ]; then
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

  if [ "$STATUS" = ok ]; then
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
    async def build(self, source: dagger.Directory, jobs: int | None = None) -> str:
        """REAL — the C/C++ BUILD LEG (not a scan check): patch -> configure -> make (FR-73).

        Fails when patch/configure/make fails or no binary is produced.
        Warnings are counted and reported; they do not fail the check.
        """
        if jobs:
            script = BUILD_SCRIPT.replace('make -j"$(nproc)"', f"make -j{jobs}")
        else:
            script = BUILD_SCRIPT
        ctr = (
            _toolchain()
            .with_mounted_directory("/src", source)
            .with_workdir("/src")
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
