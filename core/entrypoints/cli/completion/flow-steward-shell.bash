# Interactive Flow Steward shell: `flow-steward shell` (or ./flow-steward shell).
#
# Type a command without the program name -- `users list`, `maintenance status` --
# and press Tab to complete commands, options and choices. Up/Down walk the history.
# Every command runs through the same launcher as `flow-steward <command>`, so it goes
# to the right runtime on Compact, Scalable and a provider console alike.
# `help` lists the commands, `exit` or Ctrl+D leaves. Ordinary shell commands still work.

[ -r /etc/bash.bashrc ] && . /etc/bash.bashrc
. "${FS_SHELL_COMPLETION_DIR:?}/flow-steward.bash"

_flow_steward_shell_complete() {
    COMP_WORDS=(flow-steward "${COMP_WORDS[@]}")
    COMP_CWORD=$((COMP_CWORD + 1))
    _flow_steward_complete
}

for _flow_steward_name in "${_FLOW_STEWARD_COMMANDS[@]}"; do
    eval "${_flow_steward_name}() { \"\$FS_SHELL_PROGRAM\" ${_flow_steward_name} \"\$@\"; }"
    complete -o default -F _flow_steward_shell_complete "${_flow_steward_name}"
done
unset _flow_steward_name
# bash 5 can complete the first word too: `us<Tab>` -> `users`.
if [ "${BASH_VERSINFO[0]:-0}" -ge 5 ]; then
    complete -I -W "${_FLOW_STEWARD_COMMANDS[*]} help exit" 2>/dev/null
fi

help() {
    if [ "$#" -eq 0 ]; then
        "$FS_SHELL_PROGRAM" --help
    else
        "$FS_SHELL_PROGRAM" "$@" --help
    fi
}

HISTFILE="${FS_SHELL_HISTORY:-$HOME/.flow_steward_history}"
HISTSIZE=2000
HISTCONTROL=ignoredups
shopt -s histappend 2>/dev/null
PROMPT_COMMAND='history -a'
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    PS1='\[\033[1;36m\]flow-steward\[\033[0m\]> '
else
    PS1='flow-steward> '
fi
printf '%s\n' "Flow Steward shell: type a command (Tab completes it), \`help\` for the list, \`exit\` to leave."
