#!/bin/sh
# Render the non-secret, closed descriptor for the reusable CLI runtime.

set -eu

fail() {
    printf '%s\n' 'invalid CLI runtime identity descriptor' >&2
    exit 2
}

compose_project=
stack_environment=
active_revision=
compose_files=
env_file=
host_project_root=
host_uid=
host_gid=
docker_gid=
seen_compose_project=false
seen_stack_environment=false
seen_active_revision=false
seen_compose_files=false
seen_env_file=false
seen_host_project_root=false
seen_host_uid=false
seen_host_gid=false
seen_docker_gid=false
seen_env_file_digest_override=false
env_file_digest_override=
newline=$(printf '\nx')
newline=${newline%x}
carriage_return=$(printf '\rx')
carriage_return=${carriage_return%x}

while [ "$#" -gt 0 ]; do
    if [ "$1" = "--env-file-digest-from-environment" ]; then
        [ "$seen_env_file_digest_override" = false ] || fail
        env_file_digest_override=${FS_CLI_ENV_FILE_DIGEST-}
        seen_env_file_digest_override=true
        shift
        continue
    fi
    [ "$#" -ge 2 ] || fail

    case "$1" in
        --compose-project)
            [ "$seen_compose_project" = false ] || fail
            compose_project=$2
            seen_compose_project=true
            ;;
        --stack-environment)
            [ "$seen_stack_environment" = false ] || fail
            stack_environment=$2
            seen_stack_environment=true
            ;;
        --active-revision)
            [ "$seen_active_revision" = false ] || fail
            active_revision=$2
            seen_active_revision=true
            ;;
        --compose-files)
            [ "$seen_compose_files" = false ] || fail
            compose_files=$2
            seen_compose_files=true
            ;;
        --env-file)
            [ "$seen_env_file" = false ] || fail
            env_file=$2
            seen_env_file=true
            ;;
        --host-project-root)
            [ "$seen_host_project_root" = false ] || fail
            host_project_root=$2
            seen_host_project_root=true
            ;;
        --host-uid)
            [ "$seen_host_uid" = false ] || fail
            host_uid=$2
            seen_host_uid=true
            ;;
        --host-gid)
            [ "$seen_host_gid" = false ] || fail
            host_gid=$2
            seen_host_gid=true
            ;;
        --docker-gid)
            [ "$seen_docker_gid" = false ] || fail
            docker_gid=$2
            seen_docker_gid=true
            ;;
        *)
            fail
            ;;
    esac
    shift 2
done

for field in \
    "$compose_project" "$stack_environment" "$active_revision" "$compose_files" \
    "$env_file" "$host_project_root" \
    "$host_uid" "$host_gid" "$docker_gid"; do
    [ -n "$field" ] || fail
    case "$field" in *"$carriage_return"* | *"$newline"*) fail ;; esac
done

case "$compose_project" in
    *[!A-Za-z0-9_.-]* | .* | -*) fail ;;
esac
case "$stack_environment" in
    *[!A-Za-z0-9_.-]* | .* | -*) fail ;;
esac
case "$active_revision" in
    initial) ;;
    ????????????????????????????????????????????????????????????????)
        case "$active_revision" in *[!0123456789abcdef]*) fail ;; esac
        ;;
    *) fail ;;
esac
case "$compose_files" in
    /*) ;;
    *) fail ;;
esac
case "$env_file" in
    absent | /*) ;;
    *) fail ;;
esac
env_file_digest=absent
if [ "$env_file" != absent ]; then
    if [ "$seen_env_file_digest_override" = true ]; then
        env_file_digest=$env_file_digest_override
    else
        [ -f "$env_file" ] && [ -r "$env_file" ] || fail
        if command -v sha256sum >/dev/null 2>&1; then
            env_file_digest=$(sha256sum "$env_file" | awk '{print $1}')
        elif command -v shasum >/dev/null 2>&1; then
            env_file_digest=$(shasum -a 256 "$env_file" | awk '{print $1}')
        elif command -v openssl >/dev/null 2>&1; then
            env_file_digest=$(openssl dgst -sha256 "$env_file" | awk '{print $NF}')
        else
            printf '%s\n' 'no supported SHA-256 utility is available' >&2
            exit 127
        fi
    fi
    case "$env_file_digest" in
        ????????????????????????????????????????????????????????????????) ;;
        *) fail ;;
    esac
    case "$env_file_digest" in *[!0123456789abcdef]*) fail ;; esac
elif [ "$seen_env_file_digest_override" = true ]; then
    fail
fi
case "$host_project_root" in /*) ;; *) fail ;; esac
case "$host_uid" in *[!0123456789]* | '') fail ;; esac
case "$host_gid" in *[!0123456789]* | '') fail ;; esac
case "$docker_gid" in *[!0123456789]* | '') fail ;; esac

render_descriptor() {
    printf '%s\n' \
        'contract=1' \
        "compose_project=$compose_project" \
        "stack_environment=$stack_environment" \
        "active_revision=$active_revision" \
        "compose_files=$compose_files" \
        "env_file=$env_file" \
        "env_file_digest=$env_file_digest" \
        "host_project_root=$host_project_root" \
        "host_uid=$host_uid" \
        "host_gid=$host_gid" \
        "docker_gid=$docker_gid"
}

if command -v sha256sum >/dev/null 2>&1; then
    render_descriptor | sha256sum | awk '{print $1}'
elif command -v shasum >/dev/null 2>&1; then
    render_descriptor | shasum -a 256 | awk '{print $1}'
elif command -v openssl >/dev/null 2>&1; then
    render_descriptor | openssl dgst -sha256 | awk '{print $NF}'
else
    printf '%s\n' 'no supported SHA-256 utility is available' >&2
    exit 127
fi
