#!/usr/bin/env bash
# Report whether the MCP shared directory is usable by the container.
#
# Read-only by contract: it never elevates privilege, never creates, removes or
# chmods anything, and never runs chown. It reports state and prints the remedy
# so the privilege decision stays with the operator.
#
# The container runs as a non-root uid (10001 in the shipped image). A host
# directory owned by another uid and mode 0755 is readable but not writable, and
# the only affected tool is download_result.

set -Eeuo pipefail

usage() {
    cat <<'USAGE'
Usage: prepare-mcp-share.sh [shared-directory]

Reports ownership and access for the MCP shared directory as the service uid and
prints the remedy when a directory is unusable.
This script never modifies anything and never elevates privilege.

Arguments:
  shared-directory   Host bind source for /mcp-files. Default: ./mcp-files

Environment:
  SERVICE_UID        uid the container runs as. Default: 10001
  SERVICE_GID        gid the container runs as. Default: same as SERVICE_UID

Exit status:
  0  every probed directory satisfies what the service needs
  1  at least one directory is unusable, or the check could not run
USAGE
}

for argument in "$@"; do
    case "$argument" in
        -h|--help)
            usage
            exit 0
            ;;
    esac
done

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
shared_dir="${1:-$repo_dir/mcp-files}"
service_uid="${SERVICE_UID:-10001}"
# The shipped image creates uid and gid 10001 together; override SERVICE_GID when
# they differ on your host.
service_gid="${SERVICE_GID:-$service_uid}"

if [ ! -d "$shared_dir" ]; then
    printf 'shared directory %s does not exist\n' "$(basename -- "$shared_dir")" >&2
    printf 'the service creates it on startup; create it on the host first:\n' >&2
    printf '  sudo mkdir -p %s/input %s/output\n' "$shared_dir" "$shared_dir" >&2
    exit 1
fi

failures=0

report() {
    local directory=$1
    local need_write=$2
    local name
    name=$(basename -- "$directory")

    if [ ! -e "$directory" ]; then
        printf '%s: missing; the service creates it on startup\n' "$name"
        return 0
    fi

    local owner group mode group_bit other_bit
    owner=$(stat -c '%u' "$directory")
    group=$(stat -c '%g' "$directory")
    mode=$(stat -c '%a' "$directory")

    # Decide from the mode bits against the *service* uid. `[ -w ]` would only
    # report whether the invoking user may write, which is not the question: the
    # container runs as service_uid regardless of who runs this script.
    # Judge the permission bit, not "digit >= 2": read-only digits 4 and 5 also
    # compare >= 2. The low two bits of an rwx digit carry w, so a digit modulo
    # 4 of 2 or 3 means the permission is present.
    group_bit=$(( ${mode: -2:1} % 4 >= 2 ? 1 : 0 ))
    other_bit=$(( ${mode: -1:1} % 4 >= 2 ? 1 : 0 ))

    if [ "$owner" = "$service_uid" ] || { [ "$group" = "$service_gid" ] && [ "$group_bit" = 1 ]; } || [ "$other_bit" = 1 ]; then
        printf '%s: uid %s, mode %s, writable\n' "$name" "$owner" "$mode"
        return 0
    fi

    if [ "$need_write" = no ]; then
        # Reads are sufficient for input/: translate_file and check_status never
        # write into the share.
        printf '%s: uid %s, mode %s, readable only (sufficient: writes not needed here)\n' \
            "$name" "$owner" "$mode"
        return 0
    fi

    failures=$((failures + 1))
    printf '%s: uid %s, mode %s; the service runs as uid %s and cannot write here\n' \
        "$name" "$owner" "$mode" "$service_uid"
    if [ "$name" = output ]; then
        cat <<'REMEDY'
  The service creates this directory itself when it is missing, so the simplest
  fix is to let it:
    sudo rm -rf mcp-files/output
  Alternatively, grant the service uid access to the whole share:
    sudo chown -R 10001:10001 mcp-files
  Read-only tools (translate_file, check_status) keep working meanwhile.
REMEDY
    else
        cat <<'REMEDY'
    sudo chown -R 10001:10001 mcp-files
REMEDY
    fi
}

printf 'checking %s for service uid %s (gid %s)\n' "$shared_dir" "$service_uid" "$service_gid"
# input/ is only ever read, so read-only access there is not a fault. output/ and
# the share root must accept a download.
report "$shared_dir/input" no
# A missing output directory is healthy: the download path creates it under the
# service uid on first use. Only an existing, unwritable one is a fault.
report "$shared_dir/output" yes
report "$shared_dir" yes

if [ "$failures" -gt 0 ]; then
    printf '\n%d directory/directories unusable; downloads will fail with shared_dir_unavailable\n' \
        "$failures"
    exit 1
fi

printf '\nall probed directories satisfy what the service needs\n'