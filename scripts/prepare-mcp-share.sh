#!/usr/bin/env bash
# Report whether the MCP shared directory is usable by the container.
#
# Read-only by contract: it never elevates privilege, creates, removes, or
# changes permissions. Access is estimated from numeric UID/GID and POSIX mode
# bits. ACL entries and supplementary groups are not evaluated.

set -Eeuo pipefail

usage() {
    cat <<'USAGE'
Usage: prepare-mcp-share.sh [shared-directory]

Reports access to the MCP shared directory using SERVICE_UID/SERVICE_GID and
the directory's POSIX mode bits. The shared root and input need read/search;
output needs read/write/search. A missing output directory is usable only
when its parent has read/write/search access to create it. This checker never
modifies anything or elevates privilege.

This numeric UID/GID check does not evaluate POSIX ACLs or supplementary groups,
so it can conservatively report inaccessible a directory the service can access
through either of those mechanisms. It checks the configured root and its input
and output children, not ancestors above the configured root.

Arguments:
  shared-directory   Host bind source for /mcp-files. Default: ./mcp-files

Environment:
  SERVICE_UID        uid the container runs as. Default: 10001
  SERVICE_GID        primary gid the container runs as. Default: same as UID

Exit status:
  0  every required directory operation appears usable
  1  a required operation is unusable or could not be inspected
  2  usage or UID/GID configuration is invalid
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
service_gid="${SERVICE_GID:-$service_uid}"

normalize_id() {
    local value=$1
    while [[ ${#value} -gt 1 && $value == 0* ]]; do
        value=${value#0}
    done
    printf '%s' "$value"
}

if [[ ! $service_uid =~ ^[0-9]+$ || ! $service_gid =~ ^[0-9]+$ ]]; then
    printf 'SERVICE_UID and SERVICE_GID must be non-negative decimal integers\n' >&2
    exit 2
fi
service_uid=$(normalize_id "$service_uid")
service_gid=$(normalize_id "$service_gid")

shell_quote() {
    local escaped=${1//\'/\'\\\'\'}
    printf "'%s'" "$escaped"
}

metadata() {
    # GNU stat reports the link itself by default. Callers reject symlinks
    # before reaching this function.
    stat -c '%u:%g:%a' -- "$1" 2>/dev/null
}

access_satisfies() {
    local owner=$1 group=$2 mode=$3 required=$4
    local digits=${mode: -3} selected
    owner=$(normalize_id "$owner")
    group=$(normalize_id "$group")

    if [[ $service_uid == "$owner" ]]; then
        selected=${digits:0:1}
    elif [[ $service_gid == "$group" ]]; then
        selected=${digits:1:1}
    else
        selected=${digits:2:1}
    fi

    # The selected octal digit contains rwx as bits 4, 2, and 1.
    (( (selected & required) == required ))
}

describe_access() {
    local path=$1 label=$2 operation=$3 required=$4 repair_path=$5 recursive=$6
    local values owner group mode quoted_path quoted_ids chown_option
    local access_words

    if [[ -L $path ]]; then
        failures=$((failures + 1))
        printf '%s: symlink; refusing to follow it\n' "$label"
        return 1
    fi
    if [[ ! -e $path ]]; then
        failures=$((failures + 1))
        printf '%s: missing; this directory must exist for %s\n' "$label" "$operation"
        printf '  create it with: sudo mkdir -p %s\n' "$(shell_quote "$path")"
        return 1
    fi
    if [[ ! -d $path ]]; then
        failures=$((failures + 1))
        printf '%s: non-directory; refusing to use it for %s\n' "$label" "$operation"
        return 1
    fi

    if ! values=$(metadata "$path"); then
        failures=$((failures + 1))
        printf '%s: could not inspect ownership and mode; assuming unusable\n' "$label"
        return 1
    fi
    IFS=: read -r owner group mode <<<"$values"
    if [[ ! $mode =~ ^[0-7]{1,4}$ ]]; then
        failures=$((failures + 1))
        printf '%s: unrecognized POSIX mode; assuming unusable\n' "$label"
        return 1
    fi
    mode=$(printf '%03o' "$((8#$mode))")

    if access_satisfies "$owner" "$group" "$mode" "$required"; then
        printf '%s: uid %s, gid %s, mode %s; %s access available\n' \
            "$label" "$owner" "$group" "$mode" "$operation"
        return 0
    fi

    failures=$((failures + 1))
    case "$operation" in
        'read and search') access_words='read and search' ;;
        'read, write, and search') access_words='read, write, and search' ;;
        'read, write, and search (to create output)') access_words='read, write, and search' ;;
        *) access_words=$operation ;;
    esac
    printf '%s: uid %s, gid %s, mode %s; service uid %s gid %s does not meet required %s permissions\n' \
        "$label" "$owner" "$group" "$mode" "$service_uid" "$service_gid" "$access_words"

    quoted_path=$(shell_quote "$repair_path")
    printf '  suggested ownership remedy (not run):\n'
    quoted_ids=$(shell_quote "$service_uid:$service_gid")
    if [[ $recursive == yes ]]; then
        chown_option='-R '
    else
        chown_option=''
    fi
    printf '    sudo chown %s%s %s\n' "$chown_option" "$quoted_ids" "$quoted_path"
    case "$operation" in
        'read and search')
            printf '    sudo chmod u+rx %s\n' "$quoted_path"
            ;;
        'read, write, and search'|'read, write, and search (to create output)')
            printf '    sudo chmod u+rwx %s\n' "$quoted_path"
            ;;
    esac
    if [[ $label == output ]]; then
        printf '  destructive alternative, only if the parent lets the service recreate output:\n'
        printf '    sudo rm -rf -- %s\n' "$quoted_path"
        printf '  warning: this permanently deletes existing output artifacts.\n'
    fi
    return 1
}

failures=0
printf 'checking %s for service uid %s (gid %s)\n' \
    "$(shell_quote "$shared_dir")" "$service_uid" "$service_gid"

if [[ -L $shared_dir ]]; then
    printf 'shared directory: symlink; refusing to follow it\n'
    exit 1
fi
if [[ ! -e $shared_dir ]]; then
    printf 'shared directory %s does not exist\n' "$(shell_quote "$shared_dir")"
    printf '  create it with: sudo mkdir -p %s/input %s/output\n' \
        "$(shell_quote "$shared_dir")" "$(shell_quote "$shared_dir")"
    exit 1
fi
if [[ ! -d $shared_dir ]]; then
    printf 'shared directory: non-directory; refusing to use it\n'
    exit 1
fi

output_path="$shared_dir/output"
# open_shared_directory opens the root and child directories with O_RDONLY |
# O_DIRECTORY. Therefore existing directories require read+search, and output
# creation also needs write on the shared root.
if [[ ! -e $output_path && ! -L $output_path ]]; then
    # open_shared_directory(create=True) creates output under the share root.
    describe_access "$shared_dir" 'shared directory (output parent)' \
        'read, write, and search (to create output)' 7 "$shared_dir" no || :
else
    describe_access "$shared_dir" 'shared directory' 'read and search' 5 \
        "$shared_dir" no || :
fi

describe_access "$shared_dir/input" input 'read and search' 5 "$shared_dir/input" yes || :
if [[ ! -e $output_path && ! -L $output_path ]]; then
    printf 'output: missing; service can create it under the checked parent\n'
else
    describe_access "$output_path" output 'read, write, and search' 7 "$output_path" no || :
fi

if [ "$failures" -gt 0 ]; then
    printf '\n%d directory operation(s) unusable; downloads or input reads may fail\n' "$failures"
    exit 1
fi

printf '\nall probed directory operations satisfy what the service needs\n'
