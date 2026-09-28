#!/usr/bin/env bash
# Test harness for install.sh. Plain bash, no bats/frameworks.
#
# Builds a sandbox with fake executables (apt-get, raspi-config, arecord,
# git, uname on PATH; id/getent/usermod are thin logging wrappers around
# the real binaries so they act on a real, ephemeral test user; python3 is
# faked only to observe the installed wrapper's argv) and runs the real
# install.sh against it with GSP_INSTALL_DIR / GSP_BIN_PATH pointed into
# the sandbox instead of /opt and /usr/local/bin. Prints PASS/FAIL per
# assertion and exits non-zero if anything failed.
#
# Must be run as root (it creates a temporary system user and groups).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_SH="$REPO_DIR/install.sh"
FAKES_DIR="$SCRIPT_DIR/install_fakes"

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "Cannot find install.sh at $INSTALL_SH" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "This test harness must be run as root (it creates a temp user/groups)." >&2
    exit 1
fi

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

assert_exit_eq() { # actual expected label
    if [[ "$1" -eq "$2" ]]; then pass "$3 (exit $1)"; else fail "$3 (exit was $1, expected $2)"; fi
}
assert_exit_ne() { # actual not-expected label
    if [[ "$1" -ne "$2" ]]; then pass "$3 (exit $1)"; else fail "$3 (exit was $1, expected != $2)"; fi
}
assert_contains() { # haystack needle label
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 -- did not find: $2"; fi
}
assert_not_contains() { # haystack needle label
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3 -- unexpectedly found: $2"; fi
}
assert_no_call() { # log tool_name label -- no line in the call log starts with "tool_name "
    if grep -qE "^$2 " <<< "$1"; then
        fail "$3 -- unexpectedly found: $(grep -E "^$2 " <<< "$1" | head -1)"
    else
        pass "$3"
    fi
}

# --------------------------------------------------------------------------- #
# Sandbox setup
SANDBOX="$(mktemp -d /tmp/gsp-install-test.XXXXXX)"
CALL_LOG="$SANDBOX/calls.log"
touch "$CALL_LOG"
export CALL_LOG

# PATH with all fakes.
PATH_FAKES="$FAKES_DIR:$PATH"

# A second PATH variant with every fake except raspi-config, to simulate a
# system where raspi-config isn't installed.
NO_RC_DIR="$SANDBOX/fakebin_no_raspiconfig"
mkdir -p "$NO_RC_DIR"
for f in "$FAKES_DIR"/*; do
    name="$(basename "$f")"
    [[ "$name" == "raspi-config" ]] && continue
    ln -s "$f" "$NO_RC_DIR/$name"
done
PATH_NO_RC="$NO_RC_DIR:$PATH"

# A real, ephemeral system user + groups so id/getent/usermod/install -o/-g
# (all real binaries, or thin logging wrappers around them) behave
# correctly, without touching any real person's account.
TEST_USER="gsptestusr$$"
TEST_HOME="$SANDBOX/home/$TEST_USER"
useradd -M -d "$TEST_HOME" "$TEST_USER"

CREATED_GROUPS=()
for g in gpio spi i2c; do
    if ! getent group "$g" >/dev/null 2>&1; then
        groupadd "$g"
        CREATED_GROUPS+=("$g")
    fi
done

cleanup() {
    userdel "$TEST_USER" >/dev/null 2>&1
    for g in "${CREATED_GROUPS[@]}"; do
        groupdel "$g" >/dev/null 2>&1
    done
    rm -rf "$SANDBOX"
}
trap cleanup EXIT

reset_case_env() {
    unset FAKE_KERNEL FAKE_GIT_BRANCHES FAKE_CARD_PRESENT \
          APT_FAIL_PATTERNS APT_FAIL_ALL SUDO_USER 2>/dev/null || true
}
reset_log() { : > "$CALL_LOG"; }

OUT=""
RC=0

# do_run PATH_VAR INSTALL_DIR BIN_PATH ARGS...
# Runs the real install.sh. Sets globals OUT (combined stdout+stderr) and
# RC (exit code). Reads currently-exported FAKE_*/APT_FAIL_*/SUDO_USER.
do_run() {
    local use_path="$1" idir="$2" bpath="$3"
    shift 3
    # On a real box /usr/local/bin already exists; emulate that for
    # whatever sandbox directory GSP_BIN_PATH points into.
    mkdir -p "$(dirname "$bpath")"
    OUT="$(PATH="$use_path" GSP_INSTALL_DIR="$idir" GSP_BIN_PATH="$bpath" "$INSTALL_SH" "$@" 2>&1)"
    RC=$?
}

# do_run_script SCRIPT PATH_VAR INSTALL_DIR BIN_PATH ARGS...
# Same, but against an explicit script path (used for the "installer
# copied without its analyzer" case).
do_run_script() {
    local script="$1" use_path="$2" idir="$3" bpath="$4"
    shift 4
    mkdir -p "$(dirname "$bpath")"
    OUT="$(PATH="$use_path" GSP_INSTALL_DIR="$idir" GSP_BIN_PATH="$bpath" "$script" "$@" 2>&1)"
    RC=$?
}

echo "Sandbox: $SANDBOX"
echo "Test user: $TEST_USER (home: $TEST_HOME)"
echo

# =========================================================================== #
echo "--- C1: non-root refusal ---"
reset_case_env; reset_log
idir="$SANDBOX/c1/opt"; bpath="$SANDBOX/c1/bin/gsp-keystudio"
mkdir -p "$(dirname "$bpath")"
OUT="$(setpriv --reuid=65534 --regid=65534 --clear-groups \
        env PATH="$PATH_FAKES" GSP_INSTALL_DIR="$idir" GSP_BIN_PATH="$bpath" \
        "$INSTALL_SH" 2>&1)"
RC=$?
assert_exit_eq "$RC" 1 "C1: non-root exits 1"
assert_contains "$OUT" "sudo" "C1: message mentions sudo"
if [[ ! -e "$idir" ]]; then pass "C1: install dir was not created"; else fail "C1: install dir was not created"; fi
if [[ ! -s "$CALL_LOG" ]]; then pass "C1: no fake tool was invoked"; else fail "C1: no fake tool was invoked -- log: $(cat "$CALL_LOG")"; fi

# =========================================================================== #
echo "--- C2: missing respeaker_analyzer.py next to installer ---"
reset_case_env; reset_log
c2dir="$SANDBOX/c2_src"
mkdir -p "$c2dir"
cp "$INSTALL_SH" "$c2dir/install.sh"
chmod 755 "$c2dir/install.sh"
idir="$SANDBOX/c2/opt"; bpath="$SANDBOX/c2/bin/gsp-keystudio"
do_run_script "$c2dir/install.sh" "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 1 "C2: exits 1 when analyzer missing"
assert_contains "$OUT" "respeaker_analyzer.py" "C2: message names the missing file"
if [[ ! -s "$CALL_LOG" ]]; then pass "C2: nothing was installed (no calls logged)"; else fail "C2: nothing was installed (no calls logged)"; fi

# =========================================================================== #
echo "--- C3: option parsing (--help / unknown option) ---"
reset_case_env; reset_log
idir="$SANDBOX/c3/opt"; bpath="$SANDBOX/c3/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath" --this-is-not-real
assert_exit_eq "$RC" 1 "C3: unknown option exits 1"
assert_contains "$OUT" "Unknown option" "C3: unknown option message"

do_run "$PATH_FAKES" "$idir" "$bpath" --help
assert_exit_eq "$RC" 0 "C3: --help exits 0"
assert_contains "$OUT" "sudo ./install.sh" "C3: --help prints usage"
assert_not_contains "$OUT" "set -euo pipefail" "C3: --help does not leak script code (sed range regression)"
if [[ ! -s "$CALL_LOG" ]]; then pass "C3: --help/unknown option installed nothing"; else fail "C3: --help/unknown option installed nothing"; fi

# =========================================================================== #
echo "--- C4: --no-driver full flow ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
idir="$SANDBOX/c4/opt"; bpath="$SANDBOX/c4/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath" --no-driver
assert_exit_eq "$RC" 0 "C4: --no-driver succeeds"
log="$(cat "$CALL_LOG")"
assert_contains "$log" "apt-get install -y alsa-utils python3 python3-numpy python3-rpi.gpio python3-spidev git dkms i2c-tools" "C4: apt-get install called with the right package list"
assert_contains "$log" "raspi-config nonint do_i2c 0" "C4: raspi-config do_i2c 0 called"
assert_contains "$log" "raspi-config nonint do_spi 0" "C4: raspi-config do_spi 0 called"
assert_not_contains "$log" "git clone" "C4: git never called with --no-driver"

if [[ -x "$bpath" ]]; then pass "C4: wrapper created and executable"; else fail "C4: wrapper created and executable"; fi
if [[ -f "$idir/respeaker_analyzer.py" ]]; then pass "C4: analyzer copied into install dir"; else fail "C4: analyzer copied into install dir"; fi
if cmp -s "$idir/respeaker_analyzer.py" "$REPO_DIR/respeaker_analyzer.py"; then pass "C4: copied analyzer matches source"; else fail "C4: copied analyzer matches source"; fi

wrap_out="$(PATH="$PATH_FAKES" "$bpath" --file "my file.wav" --now 2>&1)"
assert_contains "$wrap_out" "ARG:[--file]" "C4: wrapper passes through --file"
assert_contains "$wrap_out" "ARG:[my file.wav]" "C4: wrapper passes through an arg containing spaces intact"
assert_contains "$wrap_out" "ARG:[--now]" "C4: wrapper passes through --now"

log="$(cat "$CALL_LOG")"
assert_contains "$log" "usermod -aG audio $TEST_USER" "C4: usermod -aG audio for target user"
assert_contains "$log" "usermod -aG gpio $TEST_USER" "C4: usermod -aG gpio for target user"
assert_contains "$log" "usermod -aG spi $TEST_USER" "C4: usermod -aG spi for target user"
assert_contains "$log" "usermod -aG i2c $TEST_USER" "C4: usermod -aG i2c for target user"

rec_dir="$TEST_HOME/recordings"
if [[ -d "$rec_dir" ]]; then pass "C4: recordings dir created in user's home"; else fail "C4: recordings dir created in user's home"; fi
owner="$(stat -c '%U' "$rec_dir" 2>/dev/null || echo '?')"
if [[ "$owner" == "$TEST_USER" ]]; then pass "C4: recordings dir owned by target user"; else fail "C4: recordings dir owned by target user (was: $owner)"; fi

assert_contains "$OUT" "Log out" "C4: final message tells user to log out"
assert_not_contains "$OUT" "sudo reboot" "C4: final message does not tell user to reboot (no driver installed)"
assert_not_contains "$OUT" "was just installed" "C4: final message does not claim the driver was installed"
unset SUDO_USER

# =========================================================================== #
echo "--- C5: driver path, card absent, kernel 6.6.51+rpt-rpi-v8, branch v6.6 exists ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export FAKE_KERNEL="6.6.51+rpt-rpi-v8"
export FAKE_GIT_BRANCHES="v6.6,v6.1,v5.15"
export FAKE_CARD_PRESENT=0
idir="$SANDBOX/c5/opt"; bpath="$SANDBOX/c5/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 0 "C5: driver install flow succeeds"
log="$(cat "$CALL_LOG")"
assert_contains "$log" "git clone https://github.com/HinTak/seeed-voicecard" "C5: driver repo cloned"
assert_contains "$log" "git ls-remote --exit-code --heads origin v6.6" "C5: correct kernel branch checked (v6.6)"
assert_contains "$log" "git checkout v6.6" "C5: matching branch checked out"
assert_contains "$log" "driver-install.sh" "C5: driver's own install.sh executed"
assert_not_contains "$OUT" "No driver branch" "C5: no missing-branch warning when branch exists"
clone_dest="$(grep '^git clone' "$CALL_LOG" | awk '{print $NF}')"
if [[ -n "$clone_dest" && ! -d "$clone_dest" ]]; then pass "C5: temp clone dir was cleaned up"; else fail "C5: temp clone dir was cleaned up (dest: $clone_dest)"; fi
assert_contains "$OUT" "sudo reboot" "C5: final message tells user to reboot"

# =========================================================================== #
echo "--- C6: driver path, branch missing for kernel, falls back to default branch ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export FAKE_KERNEL="9.9.9-custom"
export FAKE_GIT_BRANCHES="v6.6,v6.1"
export FAKE_CARD_PRESENT=0
idir="$SANDBOX/c6/opt"; bpath="$SANDBOX/c6/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 0 "C6: driver install flow still succeeds"
assert_contains "$OUT" "No driver branch for kernel v9.9" "C6: warns about missing branch"
log="$(cat "$CALL_LOG")"
assert_not_contains "$log" "git checkout v9.9" "C6: does not check out a nonexistent branch"
assert_contains "$log" "driver-install.sh" "C6: driver install still runs on default branch"
assert_contains "$OUT" "sudo reboot" "C6: final message tells user to reboot"

# =========================================================================== #
echo "--- C7: card already present -- driver install skipped ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export FAKE_CARD_PRESENT=1
idir="$SANDBOX/c7/opt"; bpath="$SANDBOX/c7/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 0 "C7: succeeds when card already present"
assert_contains "$OUT" "already detected" "C7: reports card already detected"
log="$(cat "$CALL_LOG")"
assert_no_call "$log" "git" "C7: git never called when card already present"
assert_not_contains "$log" "linux-headers" "C7: kernel headers not installed when card already present"
assert_contains "$log" "apt-get install -y alsa-utils" "C7: main package set still installed"
assert_contains "$OUT" "Log out" "C7: final message says log out"
assert_not_contains "$OUT" "sudo reboot" "C7: no reboot message when driver wasn't (re)installed"
assert_not_contains "$OUT" "was just installed" "C7: message does not claim the driver was installed"

# =========================================================================== #
echo "--- C8: kernel headers -- first package name fails, falls back and succeeds ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export FAKE_KERNEL="6.6.51+rpt-rpi-v8"
export FAKE_GIT_BRANCHES="v6.6"
export FAKE_CARD_PRESENT=0
export APT_FAIL_PATTERNS="linux-headers-"
idir="$SANDBOX/c8/opt"; bpath="$SANDBOX/c8/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 0 "C8: succeeds overall"
log="$(cat "$CALL_LOG")"
assert_contains "$log" "apt-get install -y linux-headers-6.6.51+rpt-rpi-v8" "C8: first headers package attempted"
assert_contains "$log" "apt-get install -y raspberrypi-kernel-headers" "C8: falls back to raspberrypi-kernel-headers"
assert_not_contains "$OUT" "Could not install kernel headers" "C8: no headers warning when fallback succeeds"

# =========================================================================== #
echo "--- C9: kernel headers -- both package names fail ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export FAKE_KERNEL="6.6.51+rpt-rpi-v8"
export FAKE_GIT_BRANCHES="v6.6"
export FAKE_CARD_PRESENT=0
export APT_FAIL_PATTERNS="linux-headers-,raspberrypi-kernel-headers"
idir="$SANDBOX/c9/opt"; bpath="$SANDBOX/c9/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_eq "$RC" 0 "C9: continues (exit 0) even when both headers packages fail"
assert_contains "$OUT" "Could not install kernel headers" "C9: warns when both headers packages fail"
log="$(cat "$CALL_LOG")"
assert_contains "$log" "driver-install.sh" "C9: driver install still runs after headers warning"

# =========================================================================== #
echo "--- C10: a failing step stops the script (set -e) before the wrapper is made ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
export APT_FAIL_PATTERNS="alsa-utils"
idir="$SANDBOX/c10/opt"; bpath="$SANDBOX/c10/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath"
assert_exit_ne "$RC" 0 "C10: script exits non-zero when a required step fails"
log="$(cat "$CALL_LOG")"
assert_contains "$log" "apt-get update" "C10: apt-get update did run"
assert_contains "$log" "apt-get install -y alsa-utils" "C10: the failing install was attempted"
if [[ ! -e "$bpath" ]]; then pass "C10: wrapper was not created"; else fail "C10: wrapper was not created"; fi
assert_not_contains "$log" "usermod" "C10: never reached the group/user setup step"

# =========================================================================== #
echo "--- C11: idempotent -- running twice succeeds and the wrapper still works ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
idir="$SANDBOX/c11/opt"; bpath="$SANDBOX/c11/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath" --no-driver
first_rc=$RC
do_run "$PATH_FAKES" "$idir" "$bpath" --no-driver
second_rc=$RC
assert_exit_eq "$first_rc" 0 "C11: first run succeeds"
assert_exit_eq "$second_rc" 0 "C11: second run succeeds"
wrap_out="$(PATH="$PATH_FAKES" "$bpath" --ping 2>&1)"
assert_contains "$wrap_out" "ARG:[--ping]" "C11: wrapper still works correctly after re-install"

# =========================================================================== #
echo "--- C12: SUDO_USER unset defaults to pranav; missing user warns without crashing ---"
reset_case_env; reset_log
idir="$SANDBOX/c12/opt"; bpath="$SANDBOX/c12/bin/gsp-keystudio"
do_run "$PATH_FAKES" "$idir" "$bpath" --no-driver
assert_exit_eq "$RC" 0 "C12: succeeds even though default user doesn't exist"
assert_contains "$OUT" "Giving pranav access" "C12: defaults target user to pranav when SUDO_USER is unset"
assert_contains "$OUT" "User pranav not found" "C12: warns that pranav is missing"
assert_not_contains "$(cat "$CALL_LOG")" "usermod" "C12: no usermod calls for a nonexistent user"
if [[ ! -e "/home/pranav" ]]; then pass "C12: real /home/pranav was never touched"; else fail "C12: real /home/pranav was never touched"; fi

# =========================================================================== #
echo "--- C13: raspi-config missing -- warns and continues ---"
reset_case_env; reset_log
export SUDO_USER="$TEST_USER"
idir="$SANDBOX/c13/opt"; bpath="$SANDBOX/c13/bin/gsp-keystudio"
do_run "$PATH_NO_RC" "$idir" "$bpath" --no-driver
assert_exit_eq "$RC" 0 "C13: succeeds when raspi-config is missing"
assert_contains "$OUT" "raspi-config not found" "C13: warns raspi-config not found"
if [[ -x "$bpath" ]]; then pass "C13: install still completed (wrapper exists)"; else fail "C13: install still completed (wrapper exists)"; fi

# =========================================================================== #
echo "--- C14: a group that doesn't exist on the system is skipped, not passed to usermod ---"
if [[ " ${CREATED_GROUPS[*]:-} " == *" spi "* ]]; then
    groupdel spi >/dev/null 2>&1
    reset_case_env; reset_log
    export SUDO_USER="$TEST_USER"
    idir="$SANDBOX/c14/opt"; bpath="$SANDBOX/c14/bin/gsp-keystudio"
    do_run "$PATH_FAKES" "$idir" "$bpath" --no-driver
    assert_exit_eq "$RC" 0 "C14: succeeds even when a target group is absent"
    log="$(cat "$CALL_LOG")"
    assert_not_contains "$log" "usermod -aG spi" "C14: usermod not called for the missing spi group"
    assert_contains "$log" "usermod -aG audio $TEST_USER" "C14: usermod still called for groups that do exist"
    groupadd spi >/dev/null 2>&1
else
    echo "SKIP: C14 (spi group pre-existed on this system; not safe to delete it for the test)"
fi

# =========================================================================== #
echo
echo "================================================================"
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
if [[ $FAIL_COUNT -eq 0 ]]; then
    echo "ALL PASS"
    exit 0
else
    echo "THERE WERE FAILURES"
    exit 1
fi
