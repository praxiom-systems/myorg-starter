#!/usr/bin/env python3
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.
"""
Bash command validator for Claude Code PreToolUse hook.
Reads patterns from TOML config and validates commands.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_log
from bash_policy_patterns import (
    CompiledPattern,
    compile_patterns,
    detect_os,
    inject_git_root_patterns,
    load_config,
    merge_os_config,
)
from myorg_message import format_reason


def strip_env_vars(cmd: str) -> str:
    """Strip environment variable assignments from command start.

    Handles: VAR=value, VAR="value", VAR='value', VAR=$(cmd), VAR=$VAR
    """
    while True:
        cmd = cmd.lstrip()
        match = re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', cmd)
        if not match:
            break

        rest = cmd[match.end():]

        if rest.startswith('$('):
            # Command substitution $(...)
            depth = 1
            i = 2
            while depth > 0 and i < len(rest):
                if rest[i] == '(':
                    depth += 1
                elif rest[i] == ')':
                    depth -= 1
                i += 1
            cmd = rest[i:]
        elif rest.startswith('`'):
            # Backtick substitution
            end = rest.find('`', 1)
            cmd = rest[end + 1:] if end > 0 else ""
        elif rest.startswith('"'):
            # Double-quoted value
            i = 1
            while i < len(rest):
                if rest[i] == '\\' and i + 1 < len(rest):
                    i += 2
                    continue
                if rest[i] == '"':
                    break
                i += 1
            cmd = rest[i + 1:]
        elif rest.startswith("'"):
            # Single-quoted value
            end = rest.find("'", 1)
            cmd = rest[end + 1:] if end > 0 else ""
        elif rest.startswith('$') and len(rest) > 1 and re.match(r'[A-Za-z_]', rest[1]):
            # Variable reference $VAR
            var_match = re.match(r'^\$[A-Za-z_][A-Za-z0-9_]*', rest)
            cmd = rest[var_match.end():] if var_match else rest
        else:
            # Unquoted value - ends at whitespace
            val_match = re.match(r'^[^\s]*\s*', rest)
            cmd = rest[val_match.end():] if val_match else ""

    return cmd.lstrip()


def strip_leading_comment(cmd: str) -> str:
    """Strip shell comments from the start of a command.

    Handles multi-line commands where the first line is a comment.
    """
    lines = cmd.split('\n')
    while lines and lines[0].strip().startswith('#'):
        lines.pop(0)
    return '\n'.join(lines).lstrip()


def _find_matching_paren(cmd: str, start: int) -> int:
    """Find the matching ')' for a '(' that was just consumed.

    Tracks nested parentheses, quotes (single and double), and heredoc bodies
    so that a ')' inside a heredoc is not mistaken for the closing paren.
    Returns the index one past the matching ')'.
    Returns len(cmd) if unmatched (unclosed substitution).
    """
    depth = 1
    i = start
    inner_quote = None
    heredoc_delim = None  # active heredoc delimiter (str) inside $()

    while i < len(cmd) and depth > 0:
        char = cmd[i]

        # --- Heredoc mode inside $(): consume body until delimiter ---
        if heredoc_delim is not None:
            if char == '\n':
                next_nl = cmd.find('\n', i + 1)
                if next_nl == -1:
                    next_nl = len(cmd)
                raw_line = cmd[i + 1:next_nl]
                # Use exact match only (<<- stripping is unusual inside $(); treat
                # conservatively and do not strip tabs so we don't close too early).
                if raw_line == heredoc_delim:
                    i = next_nl  # skip to end of delimiter line
                    heredoc_delim = None
                    continue
            i += 1
            continue

        # Skip escaped characters — but NOT inside single quotes, where backslash
        # has no special meaning in bash (\' does NOT escape the closing single quote).
        if char == '\\' and i + 1 < len(cmd) and inner_quote != "'":
            i += 2
            continue

        if char in ('"', "'"):
            if inner_quote is None:
                inner_quote = char
            elif inner_quote == char:
                inner_quote = None
        elif inner_quote is None:
            # Detect heredoc operator << (but not <<<) to enter heredoc mode
            if char == '<' and cmd[i:i+2] == '<<' and (i + 2 >= len(cmd) or cmd[i + 2] != '<'):
                delim, _strip, end_pos = _parse_heredoc_delim(cmd, i + 2)
                if delim:
                    i = end_pos
                    heredoc_delim = delim
                    continue
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1

        i += 1

    return i


def _parse_heredoc_delim(cmd: str, pos: int) -> tuple:
    """Parse a heredoc delimiter starting after '<<'.

    Handles: <<DELIM, <<'DELIM', <<"DELIM", <<-DELIM, <<-'DELIM'
    Returns (delimiter_str, strip_tabs, end_pos) or (None, False, pos) if invalid.
    strip_tabs is True for <<- heredocs (bash strips leading tabs from body lines
    and the delimiter line); False for regular << (exact delimiter match required).
    """
    j = pos
    # Check for optional '-' (<<- strips leading tabs from body and delimiter)
    strip_tabs = False
    if j < len(cmd) and cmd[j] == '-':
        strip_tabs = True
        j += 1
    # Skip optional whitespace between << / <<- and the delimiter
    while j < len(cmd) and cmd[j] in ' \t':
        j += 1
    if j >= len(cmd) or cmd[j] == '\n':
        return None, False, pos

    if cmd[j] in ("'", '"'):
        # Quoted delimiter: <<'EOF' or <<"EOF"
        delim_quote = cmd[j]
        k = cmd.find(delim_quote, j + 1)
        if k > j + 1:
            return cmd[j + 1:k], strip_tabs, k + 1
        return None, False, pos
    else:
        # Unquoted delimiter: word characters only
        m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', cmd[j:])
        if m:
            return m.group(0), strip_tabs, j + m.end()
        return None, False, pos


def split_commands(cmd: str) -> list[str]:
    """Split command on &&, ||, ;, newlines (respecting quotes, comments, and shell syntax).

    Special handling for:
    - ;; (case statement terminator) - not a split point
    - Quoted strings
    - Bare newlines are command separators (like ;) in shell
    - $(...) command substitutions - consumed as atomic chunks
    - Here-documents (<< DELIM) - content consumed until delimiter line
    """
    segments = []
    current = ""
    quote = None
    i = 0
    heredoc_delim = None       # Active heredoc delimiter string, or None
    heredoc_strip_tabs = False  # True for <<- (strip leading tabs from delimiter line)
    array_depth = 0             # Parenthesis depth inside NAME=(...) array assignment

    while i < len(cmd):
        char = cmd[i]

        # --- Heredoc mode: consume everything until the closing delimiter ---
        if heredoc_delim is not None:
            current += char
            if char == '\n':
                # Check if the next line is the heredoc delimiter.
                # For <<- heredocs, bash strips leading TABS (not spaces) from the
                # delimiter line before comparing.  For regular << heredocs the line
                # must match the delimiter exactly (no leading whitespace allowed).
                next_nl = cmd.find('\n', i + 1)
                if next_nl == -1:
                    next_nl = len(cmd)
                raw_line = cmd[i + 1:next_nl]
                candidate = raw_line.lstrip('\t') if heredoc_strip_tabs else raw_line
                if candidate == heredoc_delim:
                    # Consume the delimiter line and exit heredoc mode
                    current += raw_line
                    i = next_nl
                    heredoc_delim = None
                    heredoc_strip_tabs = False
                    continue
            i += 1
            continue

        # Track quotes (ignore escaped by odd number of backslashes)
        if char in ('"', "'"):
            backslash_count = 0
            j = i - 1
            while j >= 0 and cmd[j] == '\\':
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None

        # --- $(...) command substitution: consume as atomic chunk ---
        # Valid in unquoted context and inside double quotes (not single quotes).
        if char == '$' and i + 1 < len(cmd) and cmd[i + 1] == '(' and quote != "'":
            end = _find_matching_paren(cmd, i + 2)
            current += cmd[i:end]
            i = end
            continue

        # Split on && || ; \n outside quotes
        if quote is None:
            if array_depth > 0:
                if char == '(':
                    array_depth += 1
                elif char == ')':
                    array_depth -= 1
            elif char == '(' and re.match(r'^[A-Za-z_][A-Za-z0-9_]*(?:\+)?=$', current.strip()):
                array_depth = 1

            # --- Heredoc operator << (but not <<< here-string) ---
            if char == '<' and cmd[i:i+2] == '<<' and (i + 2 >= len(cmd) or cmd[i + 2] != '<'):
                delim, strip_tabs, end_pos = _parse_heredoc_delim(cmd, i + 2)
                if delim:
                    current += cmd[i:end_pos]
                    i = end_pos
                    heredoc_delim = delim
                    heredoc_strip_tabs = strip_tabs
                    continue
                # Not a valid heredoc, fall through to normal processing

            if array_depth == 0 and cmd[i:i+2] in ('&&', '||'):
                if current.strip():
                    segments.append(current)
                current = ""
                i += 2
                continue
            elif array_depth == 0 and char == ';':
                # Don't split on ;; (case statement terminator)
                if cmd[i:i+2] == ';;':
                    current += ';;'
                    i += 2
                    continue
                # Don't split on \; (escaped semicolon, e.g. find -exec cmd {} \;)
                if current.endswith('\\'):
                    current += char
                    i += 1
                    continue
                if current.strip():
                    segments.append(current)
                current = ""
                i += 1
                continue
            elif array_depth == 0 and char == '\n':
                # \<newline> is a line continuation in shell — do NOT split
                if current.endswith('\\'):
                    current = current[:-1]  # strip the trailing backslash
                    i += 1
                    continue
                if current.strip():
                    segments.append(current)
                current = ""
                i += 1
                continue

        current += char
        i += 1

    if current.strip():
        segments.append(current)

    return segments


# Shell control flow keywords that may prefix body commands
# These keywords introduce blocks but the body commands need separate validation
CONTROL_FLOW_KEYWORDS = re.compile(
    r'^(then|else|elif|do)\s+',
    re.IGNORECASE
)

# Shell control flow terminators that may have redirections attached
# These complete control structures and are safe on their own
# Matches: done, fi, esac — optionally followed by redirections like
# 2>/dev/null, | sort, 2>&1 | head, etc.
CONTROL_FLOW_TERMINATORS = re.compile(
    r'^(done|fi|esac)(\s+[\d<>|&].*|\s*$)',
    re.IGNORECASE
)


def extract_assignments(segment: str) -> dict[str, str]:
    """Extract VAR=literal assignments from a raw segment.

    Only captures literal values (unquoted, single-quoted, double-quoted).
    Dynamic values ($(...), backticks, $VAR references) are skipped.

    Returns dict of {name: value} for captured assignments.
    """
    env = {}
    rest = segment.lstrip()
    while True:
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', rest)
        if not match:
            break

        name = match.group(1)
        rest = rest[match.end():]

        if rest.startswith('$(') or rest.startswith('`'):
            # Dynamic command substitution — skip entire assignment, stop capturing
            break
        elif rest.startswith('${'):
            # Braced variable reference ${VAR} or ${VAR}/path — skip
            break
        elif rest.startswith('$') and len(rest) > 1 and re.match(r'[A-Za-z_]', rest[1]):
            # Variable reference $VAR — skip
            break
        elif rest.startswith('"'):
            # Double-quoted value — parse respecting escape sequences
            i = 1
            while i < len(rest):
                if rest[i] == '\\' and i + 1 < len(rest):
                    i += 2
                    continue
                if rest[i] == '"':
                    break
                i += 1
            else:
                # Unclosed double-quote: do not capture a potentially truncated value
                break
            captured = rest[1:i]
            # Reject if value contains command substitution or variable expansion
            # e.g. FOO="$(evil)" or FOO="${BAR}/path" — treat as dynamic
            if '$(' in captured or '`' in captured or '${' in captured:
                break
            env[name] = captured
            rest = rest[i + 1:]
        elif rest.startswith("'"):
            # Single-quoted value
            end = rest.find("'", 1)
            if end > 0:
                env[name] = rest[1:end]
                rest = rest[end + 1:]
            else:
                break
        else:
            # Unquoted value — ends at whitespace
            val_match = re.match(r'^([^\s]*)', rest)
            env[name] = val_match.group(1) if val_match else ""
            rest = rest[val_match.end():] if val_match else ""

        rest = rest.lstrip()

    return env


def substitute_known_vars(segment: str, env: dict[str, str]) -> str:
    """Replace $VAR or ${VAR} at position 0 of a cleaned segment with known values.

    Only substitutes if the variable name exists in env.
    Only operates at position 0 (the command itself), not arguments.
    """
    if not segment.startswith('$'):
        return segment

    # Try ${VAR} form first
    match = re.match(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}', segment)
    if match:
        name = match.group(1)
        if name in env:
            return env[name] + segment[match.end():]
        return segment

    # Try $VAR form
    match = re.match(r'^\$([A-Za-z_][A-Za-z0-9_]*)', segment)
    if match:
        name = match.group(1)
        if name in env:
            return env[name] + segment[match.end():]

    return segment


# Shell metacharacters that indicate a compound or redirecting inner command.
# If any of these appear inside a bash -c argument, the inner command is left
# unwrapped so split_commands() can process it correctly and deny patterns
# are not bypassed.
# Includes:
#   &&, ||      — logical operators
#   ;           — sequential separator
#   |           — pipe
#   $(, `       — command substitution
#   (           — subshell grouping (e.g. (rm -rf /) would bypass ^rm deny)
#   >>?, >&     — redirect operators (e.g. > /etc/cron.d/job bypasses patterns)
_SHELL_META = re.compile(r'&&|\|\||;|\||\$\(|`|\(|>>?|>&')


def strip_bash_c_wrapper(segment: str) -> str:
    """Unwrap simple bash -c / sh -c wrappers to expose the inner command.

    Handles: bash -c "cmd", bash -c 'cmd', sh -c "cmd", /bin/bash -c "cmd", etc.
    Complex quoting (nested quotes, escaped quotes inside) is left unchanged.
    Compound inner commands (containing &&, ||, ;, |, $(), backtick, subshell
    grouping, or redirects) are left unchanged so split_commands() can process
    them correctly and deny patterns are not bypassed.

    Known limitation: flags before -c (e.g. 'bash -e -c "cmd"') are NOT
    unwrapped because the regex requires -c immediately after the binary name.
    These fall back to the ask-by-default path, which is safe and conservative.
    """
    match = re.match(r'^(?:/bin/)?(bash|sh)\s+-c\s+([\'"])(.*)\2\s*$', segment, re.DOTALL)
    if not match:
        return segment

    delimiter = match.group(2)
    inner = match.group(3)

    # Reject multi-line input: embedded newlines indicate complex quoting
    if '\n' in inner or '\r' in inner:
        return segment

    # Reject if inner contains the delimiter at all (escaped or not)
    # Any occurrence means the quoting is complex enough to skip unwrapping
    if delimiter in inner:
        return segment

    # Reject compound inner commands: split_commands() handles these correctly
    # on the un-wrapped form; unwrapping here would skip that splitting and
    # let e.g. 'bash -c "echo hi && rm -rf /"' bypass deny patterns.
    if _SHELL_META.search(inner):
        return segment

    return inner


def strip_control_flow_keyword(segment: str) -> str:
    """Strip shell control flow keywords from segment start.

    When commands like 'if condition; then body; fi' are split on ';',
    we get segments like 'then body'. This function strips the 'then '
    prefix so 'body' can be validated independently.

    For terminators like 'done < file.txt', we return empty string since
    the redirection is part of the loop construct, not a separate command.

    Returns the segment with any leading control flow keyword removed,
    or empty string for terminators (which are inherently safe).
    """
    # Check for terminators first (done, fi, esac) - possibly with redirection
    if CONTROL_FLOW_TERMINATORS.match(segment):
        return ""

    # Check for body-introducing keywords (then, else, do, etc.)
    match = CONTROL_FLOW_KEYWORDS.match(segment)
    if match:
        return segment[match.end():].lstrip()

    return segment


def strip_line_continuations(cmd: str) -> str:
    r"""Strip shell line continuations (\<newline>) from command.

    In shell, a backslash followed by a newline is a line continuation
    that joins lines. Claude Code sends multi-line commands with these
    preserved in the JSON, e.g.:
        echo "test" && \
        kubectl get pods
    becomes: 'echo "test" && \\\nkubectl get pods'

    After splitting on &&, the second segment starts with '\\\n' which
    must be removed before pattern matching.
    """
    return cmd.replace('\\\n', ' ')


def _is_bare_redirect(segment: str) -> bool:
    """Check if a segment is just a bare redirect/pipe tail with no real command.

    After chain splitting, residual segments like '2>/dev/null', '| head -5',
    '2>/dev/null | head -15', or '2>&1' can appear. These are not commands
    and should be skipped.
    """
    # Strip all redirections: N>/dev/null, 2>&1, >/dev/null, N>>/path, etc.
    s = re.sub(r'\d*>>?&?\d*\s*/?[\w./-]*', '', segment)
    # Strip pipe + safe tail commands (head, tail, sort, wc, tee, grep, etc.)
    s = re.sub(r'\|\s*(head|tail|sort|wc|uniq|tee|grep|sed|awk|cut|tr|column|less|more|cat|fmt|nl|rev|paste|comm|fold|expand|unexpand|pr)\b[^|]*', '', s)
    # Strip remaining pipes to nothing
    s = re.sub(r'\|', '', s)
    return not s.strip()


_MAX_SUBSTITUTION_DEPTH = 8


def _contains_command_substitution(segment: str) -> bool:
    """Return true when a segment can execute a command during expansion."""
    return bool(_extract_command_substitutions(segment))


def _find_matching_backtick(cmd: str, start: int) -> int:
    """Find the closing backtick for a command substitution.

    Returns the index one past the closing backtick, or len(cmd) if unmatched.
    """
    i = start
    while i < len(cmd):
        if cmd[i] == '\\' and i + 1 < len(cmd):
            i += 2
            continue
        if cmd[i] == '`':
            return i + 1
        i += 1
    return len(cmd)


def _parse_heredoc_delim_for_expansion(cmd: str, pos: int) -> tuple:
    """Parse a heredoc delimiter and whether its body expands commands."""
    j = pos
    strip_tabs = False
    if j < len(cmd) and cmd[j] == '-':
        strip_tabs = True
        j += 1
    while j < len(cmd) and cmd[j] in ' \t':
        j += 1
    if j >= len(cmd) or cmd[j] == '\n':
        return None, False, False, pos

    if cmd[j] in ("'", '"'):
        delim_quote = cmd[j]
        k = cmd.find(delim_quote, j + 1)
        if k > j + 1:
            return cmd[j + 1:k], strip_tabs, True, k + 1
        return None, False, False, pos

    m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', cmd[j:])
    if m:
        return m.group(0), strip_tabs, False, j + m.end()
    return None, False, False, pos


def _skip_quoted_heredoc_body(cmd: str, delim: str, strip_tabs: bool, pos: int) -> int:
    """Skip a quoted heredoc body, where command substitutions are inert."""
    line_end = cmd.find('\n', pos)
    if line_end == -1:
        return pos

    i = line_end
    while i < len(cmd):
        next_nl = cmd.find('\n', i + 1)
        if next_nl == -1:
            next_nl = len(cmd)
        raw_line = cmd[i + 1:next_nl]
        candidate = raw_line.lstrip('\t') if strip_tabs else raw_line
        if candidate == delim:
            return next_nl + 1 if next_nl < len(cmd) else next_nl
        i = next_nl
    return len(cmd)


def _extract_command_substitutions(segment: str) -> list[str]:
    """Extract command-bearing substitution bodies from a shell segment.

    Handles command substitution ``$(...)`` and backticks plus process
    substitution ``<(...)`` / ``>(...)``. Arithmetic expansion ``$((...))`` is
    not treated as a command by itself, but executable substitutions nested
    inside the arithmetic expression are still extracted.
    """
    bodies: list[str] = []
    quote = None
    i = 0

    while i < len(segment):
        char = segment[i]

        if char == '\\' and i + 1 < len(segment) and quote != "'":
            i += 2
            continue

        if quote is None and char == '#' and (i == 0 or segment[i - 1] in ' \t'):
            next_nl = segment.find('\n', i + 1)
            if next_nl == -1:
                break
            i = next_nl + 1
            continue

        if char in ('"', "'"):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            i += 1
            continue

        if quote is None and char == '<' and segment[i:i+2] == '<<' and (
            i + 2 >= len(segment) or segment[i + 2] != '<'
        ):
            delim, strip_tabs, quoted, end_pos = _parse_heredoc_delim_for_expansion(
                segment, i + 2
            )
            if delim and quoted:
                i = _skip_quoted_heredoc_body(segment, delim, strip_tabs, end_pos)
                continue

        if char == '$' and i + 1 < len(segment) and segment[i + 1] == '(' and quote != "'":
            end = _find_matching_paren(segment, i + 2)
            if i + 2 < len(segment) and segment[i + 2] == '(':
                body_end = (
                    end - 2
                    if end >= i + 4 and segment[end - 2:end] == '))'
                    else end
                )
                bodies.extend(_extract_command_substitutions(segment[i + 3:body_end]))
                i = end
                continue
            body_end = (
                end - 1
                if end <= len(segment) and end > i + 2 and segment[end - 1] == ')'
                else end
            )
            bodies.append(segment[i + 2:body_end])
            i = end
            continue

        if char == '`' and quote != "'":
            end = _find_matching_backtick(segment, i + 1)
            body_end = (
                end - 1
                if end <= len(segment) and end > i + 1 and segment[end - 1] == '`'
                else end
            )
            bodies.append(re.sub(r'\\([$`"\\\n])', r'\1', segment[i + 1:body_end]))
            i = end
            continue

        if quote is None and char in '<>' and i + 1 < len(segment) and segment[i + 1] == '(':
            end = _find_matching_paren(segment, i + 2)
            body_end = (
                end - 1
                if end <= len(segment) and end > i + 2 and segment[end - 1] == ')'
                else end
            )
            bodies.append(segment[i + 2:body_end])
            i = end
            continue

        i += 1

    return bodies


def _is_array_assignment(segment: str) -> bool:
    """Check if a segment is a bash array assignment."""
    return bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\+)?=\(.*\)', segment.strip(), re.DOTALL))


def _is_safe_array_assignment(segment: str) -> bool:
    """Check if an array assignment contains only inert literal data.

    Newlines inside ``NAME=(...)`` are not command separators. Keep the whole
    array together in ``split_commands()`` and skip it only when its expansions
    cannot execute commands.
    """
    return _is_array_assignment(segment) and not _contains_command_substitution(segment)


def _is_safe_bash_test_expression(segment: str) -> bool:
    """Check if a segment is a side-effect-free bash conditional expression.

    ``[[ ... ]]`` is a shell keyword used as a conditional, and split chains can
    surface it as if it were a command. Treat only the predicate itself as a
    non-command fragment; command substitutions are intentionally left for the
    normal matcher so they cannot hide arbitrary commands.
    """
    s = segment.strip()
    if _contains_command_substitution(s):
        return False
    return bool(re.fullmatch(r'(?:if|elif)?\s*!?\s*\[\[.*\]\]', s, re.DOTALL))


def _strip_case_label(segment: str) -> str:
    """Strip case-statement pattern labels from segment start.

    When 'case $var in pattern) cmd ;; esac' is split on ;;/;,
    we get segments like 'pattern) cmd'. Strip the 'pattern) ' prefix
    so 'cmd' can be validated independently.

    Case labels are typically short identifiers/globs without spaces:
      foo) cmd        yes|no) cmd        *.txt) cmd        *) cmd
    We require NO spaces before ) to avoid false matches on commands
    containing (...) like 'kubectl exec ... -c "count(*) FROM ..."'.
    """
    # Match: non-space, non-$, non-newline chars followed by ) then space
    # The \S+ ensures no spaces before ), distinguishing case labels from
    # commands that happen to contain (...)
    match = re.match(r'^(\S+)\)\s+', segment)
    if match and '$(' not in match.group(1) and '(' not in match.group(1):
        return segment[match.end():]
    return segment


def clean_segment(segment: str) -> str:
    """Clean a command segment: strip whitespace, subshell chars, env vars, comments."""
    segment = segment.strip()

    # Strip residual line continuation backslash (safety net for edge cases
    # where strip_line_continuations() in validate_command() didn't catch it)
    while segment.startswith('\\') and (len(segment) == 1 or segment[1] in ' \t\n'):
        segment = segment[1:].lstrip()

    # Strip leading comments
    segment = strip_leading_comment(segment)

    if _is_array_assignment(segment):
        return "" if _is_safe_array_assignment(segment) else segment

    # Strip env vars BEFORE subshell stripping so that VAR=$(...) is consumed
    # intact. If we strip trailing ) first, it breaks the $() matching.
    segment = strip_env_vars(segment)

    # After stripping env vars, if nothing remains, it was a pure assignment
    if not segment.strip():
        return ""

    # Strip leading subshell/grouping: ( {
    while segment and segment[0] in '({':
        segment = segment[1:].lstrip()

    # Strip trailing subshell/grouping: ) }
    while segment and segment[-1] in ')}':
        segment = segment[:-1].rstrip()

    if _is_safe_bash_test_expression(segment):
        return ""

    # Check for bare redirects / pipe tails (e.g. "2>/dev/null | head -5")
    if _is_bare_redirect(segment):
        return ""

    # Unwrap bash -c / sh -c wrappers to expose the inner command
    segment = strip_bash_c_wrapper(segment)

    # Strip shell control flow keywords (then, else, do, etc.)
    # This allows validation of the body command within control structures
    segment = strip_control_flow_keyword(segment)

    # Strip case-statement pattern labels (e.g. "pattern) grep -q ...")
    segment = _strip_case_label(segment)

    # Re-run strip_env_vars after control flow stripping. A segment like
    # "do name=$(cmd)" becomes "name=$(cmd)" after keyword stripping,
    # which is now a pure assignment that should be consumed.
    segment = strip_env_vars(segment)
    if not segment.strip():
        return ""

    if _is_safe_bash_test_expression(segment):
        return ""

    return segment


def check_patterns(segment: str, patterns: list[CompiledPattern]) -> tuple[bool, str]:
    """Check if segment matches any compiled pattern.

    Returns (matched, section_name).
    """
    for pattern in patterns:
        if pattern.regex.search(segment):
            return True, pattern.section
    return False, ""


try:
    from _policy_overlay import apply_policy_gate  # type: ignore[import-not-found]
except ImportError:
    def apply_policy_gate(  # noqa: D401
        decision: str,
        reason: str,
        matched_category: str,
        matched_rule: str | None,
        cwd: str,
    ) -> tuple[str, str, str, str | None]:
        """Public starter: policy overlay not present; passthrough."""
        return decision, reason, matched_category, matched_rule


def output_decision(decision: str, reason: str, fmt: str = "claude"):
    """Emit the validator decision in a host-specific shape.

    ``claude`` (default) emits Claude Code's hookSpecificOutput JSON.
    ``raw`` emits a host-neutral ``{"decision": ..., "reason": ...}`` payload
    that per-host adapter scripts can wrap into whatever JSON their runtime
    expects (Codex, Gemini CLI, OpenCode, …) without re-implementing the
    validator itself.
    """
    if fmt == "raw":
        print(json.dumps({"decision": decision, "reason": reason}))
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason
        }
    }))


def _validate_command_substitutions(
    segment: str,
    deny_patterns: list[CompiledPattern],
    ask_patterns: list[CompiledPattern],
    allow_patterns: list[CompiledPattern],
    default_action: str,
    substitution_depth: int,
) -> list[tuple[str, str, str, str, str | None]]:
    """Validate commands embedded in command/process substitutions.

    Returns tuples of (body, decision, reason, matched_category, matched_rule).
    """
    bodies = [body.strip() for body in _extract_command_substitutions(segment)]
    bodies = [body for body in bodies if body]
    if not bodies:
        return []

    if substitution_depth >= _MAX_SUBSTITUTION_DEPTH:
        return [(
            segment[:100],
            "ask",
            "Command substitution nesting limit exceeded",
            "ask",
            None,
        )]

    results: list[tuple[str, str, str, str, str | None]] = []
    for body in bodies:
        decision, reason, matched_category, matched_rule = validate_command(
            body,
            deny_patterns,
            ask_patterns,
            allow_patterns,
            default_action,
            substitution_depth + 1,
        )
        results.append((body, decision, reason, matched_category, matched_rule))
        if decision == "deny":
            break
    return results


def validate_command(
    command: str,
    deny_patterns: list[CompiledPattern],
    ask_patterns: list[CompiledPattern],
    allow_patterns: list[CompiledPattern],
    default_action: str = "ask",
    substitution_depth: int = 0,
) -> tuple[str, str, str, str | None]:
    """Validate a command against patterns.

    Returns (decision, reason, matched_category, matched_rule).
    decision is one of: "deny", "ask", "allow"
    matched_category is one of: "deny", "ask", "allow", "default"
        — "default" means at least one segment matched no rule and the
        decision was driven by ``default_action``.
    matched_rule is the section name of the load-bearing match
        (e.g. "deny.privilege_escalation") or None if no rule matched.

    ``default_action`` controls the fallback when a segment matches no
    rule. "ask" preserves the historical behavior; "allow" makes the
    deny+ask lists the fence and silently approves anything else.
    """
    command = strip_line_continuations(command)

    matched, section = check_patterns(command, deny_patterns)
    if matched:
        reason = format_reason(
            kind="BASH",
            state="DENIED",
            rule=section,
            body_lines=[
                f"command: {command[:100]}",
                f"why:     matches {section}",
                "override: this rule cannot be overridden via env; "
                "edit blueprint/hooks/bash-patterns.toml under your user override",
            ],
        )
        return "deny", reason, "deny", section

    segments = split_commands(command)

    has_real_ask = False
    has_default_fallback = False
    ask_section: str | None = None
    ask_segment: str = ""
    ask_reason: str | None = None
    fallback_segment: str = ""
    fallback_reason: str | None = None
    allow_section: str | None = None

    env_context: dict[str, str] = {}

    for segment in segments:
        env_context.update(extract_assignments(segment))

        for (
            substitution_body,
            substitution_decision,
            substitution_reason,
            substitution_category,
            substitution_rule,
        ) in _validate_command_substitutions(
            segment,
            deny_patterns,
            ask_patterns,
            allow_patterns,
            default_action,
            substitution_depth,
        ):
            if substitution_decision == "deny":
                reason = format_reason(
                    kind="BASH",
                    state="DENIED",
                    rule=substitution_rule,
                    body_lines=[
                        f"command substitution: {substitution_body[:100]}",
                        f"why:     {substitution_reason}",
                        "override: edit blueprint/hooks/bash-patterns.toml",
                    ],
                )
                return (
                    "deny",
                    reason,
                    substitution_category,
                    substitution_rule,
                )
            if substitution_decision == "ask":
                if substitution_category == "default":
                    if not has_default_fallback:
                        has_default_fallback = True
                        fallback_segment = substitution_body
                        fallback_reason = substitution_reason
                elif not has_real_ask:
                    has_real_ask = True
                    ask_section = substitution_rule
                    ask_segment = substitution_body
                    ask_reason = format_reason(
                        kind="BASH",
                        state="ASK",
                        rule=substitution_rule,
                        body_lines=[
                            f"command: {substitution_body[:100]}",
                            f"why:     {substitution_reason}",
                            "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                        ],
                    )
            elif substitution_category == "default" and not has_default_fallback:
                has_default_fallback = True
                fallback_segment = substitution_body
                fallback_reason = substitution_reason

        cleaned = clean_segment(segment)
        if not cleaned:
            continue

        cleaned = substitute_known_vars(cleaned, env_context)

        matched, section = check_patterns(cleaned, deny_patterns)
        if matched:
            reason = format_reason(
                kind="BASH",
                state="DENIED",
                rule=section,
                body_lines=[
                    f"command: {cleaned[:100]}",
                    f"why:     matches {section}",
                    "override: this rule cannot be overridden via env; "
                    "edit blueprint/hooks/bash-patterns.toml under your user override",
                ],
            )
            return "deny", reason, "deny", section

        matched, section = check_patterns(cleaned, ask_patterns)
        if matched:
            if not has_real_ask:
                has_real_ask = True
                ask_section = section
                ask_segment = cleaned
                ask_reason = format_reason(
                    kind="BASH",
                    state="ASK",
                    rule=section,
                    body_lines=[
                        f"command: {cleaned[:100]}",
                        f"why:     matches {section}",
                        "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                    ],
                )
            continue

        matched, section = check_patterns(cleaned, allow_patterns)
        if matched:
            if allow_section is None:
                allow_section = section
            continue

        if not has_default_fallback:
            has_default_fallback = True
            fallback_segment = cleaned
            fallback_reason = None

    if has_real_ask:
        return (
            "ask",
            ask_reason or format_reason(
                kind="BASH",
                state="ASK",
                rule=ask_section,
                body_lines=[
                    f"command: {ask_segment[:100]}",
                    f"why:     matches {ask_section}",
                    "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                ],
            ),
            "ask",
            ask_section,
        )

    if has_default_fallback:
        if default_action == "allow":
            return (
                "allow",
                fallback_reason or f"'{fallback_segment}' not in any list (default-allow)",
                "default",
                None,
            )
        return (
            "ask",
            fallback_reason or f"'{fallback_segment}' not in auto-approve list",
            "default",
            None,
        )

    return "allow", "Command matches allow patterns", "allow", allow_section


def _resolve_default_action() -> str:
    """Resolve the default-action env var.

    Default is now ``allow`` — deny+ask lists are the fence and anything
    that doesn't match either is silently approved. Set ``MYORG_BASH_DEFAULT=ask``
    to restore strict behavior (unknown commands require confirmation). Anything
    other than ``ask`` resolves to ``allow`` (defensive: typos don't silently
    tighten the fence in the wrong direction).
    """
    val = os.environ.get("MYORG_BASH_DEFAULT", "allow").strip().lower()
    return "ask" if val == "ask" else "allow"


def _compute_would_action(
    actual_decision: str, matched_category: str, default_action: str
) -> str:
    """Compute what the decision would have been under the *other* default.

    The flip only matters when the load-bearing path was the default
    fallback — deny/ask/allow matches are stable across the flip.
    """
    if matched_category != "default":
        return actual_decision
    flipped = "ask" if default_action == "allow" else "allow"
    return flipped


_TOKEN_HASH_THRESHOLD = 32


def _sanitize_command(cmd: str) -> str:
    """Sanitize a command for the shadow log.

    Preserves overall command shape (so reviewers can recognize it)
    while hashing any whitespace-separated token longer than
    ``_TOKEN_HASH_THRESHOLD`` chars. Catches long bearer tokens, base64
    blobs, large path bodies, etc., without dropping the command verb.
    """
    out_tokens: list[str] = []
    for tok in cmd.split():
        if len(tok) >= _TOKEN_HASH_THRESHOLD:
            digest = hashlib.sha256(tok.encode("utf-8", errors="replace")).hexdigest()[:8]
            out_tokens.append(f"<sha8:{digest}>")
        else:
            out_tokens.append(tok)
    return " ".join(out_tokens)


def _hash_repo(cwd: str) -> str | None:
    """Return a stable opaque hash of the repo path, or None if no cwd."""
    if not cwd:
        return None
    return hashlib.sha256(cwd.encode("utf-8", errors="replace")).hexdigest()[:12]


def _write_shadow_log(row: dict) -> None:
    """Append one JSONL row to the shadow log. Failures are silent."""
    log_dir = hook_log.resolve_log_dir()
    if log_dir is None:
        return
    hook_log.append_jsonl_row(hook_log.shadow_log_path(log_dir), row)


def _build_shadow_row(
    *,
    command: str,
    cwd: str,
    input_data: dict,
    actual_decision: str,
    matched_category: str,
    matched_rule: str | None,
    would_action: str,
    default_mode: str,
) -> dict:
    return {
        "ts": int(time.time() * 1000),
        "session_id": input_data.get("session_id"),
        "repo_hash": _hash_repo(cwd),
        "agent_slug": os.environ.get("MYORG_ACTIVE_AGENT"),
        "profile": os.environ.get("MYORG_ACTIVE_PROFILE"),
        "cmd_sanitized": _sanitize_command(command),
        "cmd_sha256": hashlib.sha256(
            command.encode("utf-8", errors="replace")
        ).hexdigest(),
        "matched_rule": matched_rule,
        "matched_category": matched_category,
        "would_action": would_action,
        "actual_action": actual_decision,
        "default_mode": default_mode,
    }


_HELP_TEXT = """\
Usage: validate-bash.py <config.toml> [--format claude|raw] [--help]

Validates a bash command (read as JSON on stdin) against the deny / ask /
allow patterns in <config.toml> and emits a permission decision.

Environment variables:
  MYORG_BASH_DEFAULT=ask|allow
        Controls the fallback decision when a command segment matches no
        rule. Default is "allow" — deny+ask lists are the fence and
        anything that doesn't match either is silently approved. Set to
        "ask" to restore strict behavior (unknown commands require
        confirmation). Per-session kill switch — `export
        MYORG_BASH_DEFAULT=ask` restores strict behavior immediately.

  MYORG_HOOK_LOG_DIR=<path>
        Directory for hook logs, including the shadow log
        (<dir>/shadow.jsonl) which records what the validator *would*
        have decided under the opposite default for every invocation.
        Default: /tmp/claude-hook-logs. Empty string disables logging.

  MYORG_ACTIVE_AGENT, MYORG_ACTIVE_PROFILE
        Optional context labels included in shadow-log rows.
"""


def main():
    args = sys.argv[1:]
    fmt = "claude"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(_HELP_TEXT)
            sys.exit(0)
        if a == "--format":
            if i + 1 >= len(args):
                print(_HELP_TEXT, file=sys.stderr)
                sys.exit(1)
            fmt = args[i + 1]
            i += 2
            continue
        if a.startswith("--format="):
            fmt = a.split("=", 1)[1]
            i += 1
            continue
        positional.append(a)
        i += 1

    if len(positional) != 1 or fmt not in ("claude", "raw"):
        print(_HELP_TEXT, file=sys.stderr)
        sys.exit(1)

    config_path = positional[0]
    config = load_config(config_path)

    # Load and merge OS-specific patterns (e.g. bash-patterns.linux.toml)
    os_suffix = detect_os()
    config_dir = os.path.dirname(config_path)
    config_base = os.path.splitext(os.path.basename(config_path))[0]
    os_config_path = os.path.join(config_dir, f"{config_base}.{os_suffix}.toml")
    if os.path.isfile(os_config_path):
        os_config = load_config(os_config_path)
        config = merge_os_config(config, os_config)

    # Read JSON input first — cwd is needed for dynamic pattern injection
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Invalid input, let it pass
        sys.exit(0)

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    # If running inside a git repository, inject git-root-aware patterns before
    # compiling so that rm within the repo is auto-approved and .git is protected.
    cwd = input_data.get("cwd", "")
    if cwd:
        cwd = os.path.normpath(cwd)
        if os.path.exists(os.path.join(cwd, ".git")):
            inject_git_root_patterns(config, cwd)

    # Compile patterns (includes any git-root injections from above)
    deny_patterns = compile_patterns(config, "deny")
    ask_patterns = compile_patterns(config, "ask")
    allow_patterns = compile_patterns(config, "allow")

    default_action = _resolve_default_action()
    decision, reason, matched_category, matched_rule = validate_command(
        command, deny_patterns, ask_patterns, allow_patterns, default_action
    )

    # For force-push patterns, apply the graduated-autonomy policy gate.
    # This may upgrade the decision from ask → deny (L3, no fresh evidence)
    # or downgrade to allow (L0/L1, auto-approved). Degrades gracefully
    # when the policy overlay is absent (OSS baseline: passthrough).
    decision, reason, matched_category, matched_rule = apply_policy_gate(
        decision, reason, matched_category, matched_rule, cwd
    )

    would_action = _compute_would_action(decision, matched_category, default_action)
    _write_shadow_log(
        _build_shadow_row(
            command=command,
            cwd=cwd,
            input_data=input_data,
            actual_decision=decision,
            matched_category=matched_category,
            matched_rule=matched_rule,
            would_action=would_action,
            default_mode=default_action,
        )
    )

    output_decision(decision, reason, fmt=fmt)


if __name__ == "__main__":
    main()
