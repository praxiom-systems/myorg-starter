"""Command parsing and normalization for the bash validator.

Extracted from ``validate-bash.py`` (issue #584) — owns the *parsing* slice:
splitting compound commands on shell separators, stripping env-var
assignments, line continuations, control-flow keywords and case labels,
unwrapping ``bash -c`` wrappers, and extracting command-substitution bodies
so they can be matched against the same patterns as top-level commands.

Pure string transforms over stdlib only — no I/O, no policy state, no
dependency on the pattern slice. The validator entrypoint composes these
helpers; behavior is exercised through the validate-bash end-to-end tests.
"""

from __future__ import annotations

import re


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


def contains_command_substitution(segment: str) -> bool:
    """Return true when a segment can execute a command during expansion."""
    return bool(extract_command_substitutions(segment))


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


def extract_command_substitutions(segment: str) -> list[str]:
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
                bodies.extend(extract_command_substitutions(segment[i + 3:body_end]))
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
    return _is_array_assignment(segment) and not contains_command_substitution(segment)


def _is_safe_bash_test_expression(segment: str) -> bool:
    """Check if a segment is a side-effect-free bash conditional expression.

    ``[[ ... ]]`` is a shell keyword used as a conditional, and split chains can
    surface it as if it were a command. Treat only the predicate itself as a
    non-command fragment; command substitutions are intentionally left for the
    normal matcher so they cannot hide arbitrary commands.
    """
    s = segment.strip()
    if contains_command_substitution(s):
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
