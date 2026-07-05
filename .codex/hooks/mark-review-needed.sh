#!/bin/bash
# PostToolUse hook: 代码文件被编辑/创建后标记需要 review
# 排除已知的非代码文件，其余都触发

INPUT=$(cat)
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$ROOT" ] || exit 0
ROOT_REAL="$(cd -P "$ROOT" >/dev/null 2>&1 && pwd)"
STATE_FILE="$ROOT_REAL/.agents/.needs-review"

normalize_abs_path() {
  local path parent base parent_real
  path="$1"
  case "$path" in
    "") return 1 ;;
    /*) ;;
    *) path="$ROOT_REAL/${path#./}" ;;
  esac
  if [ -e "$path" ]; then
    if [ -d "$path" ]; then
      cd -P "$path" >/dev/null 2>&1 && pwd
      return
    fi
    parent="$(dirname "$path")"
    base="$(basename "$path")"
    parent_real="$(cd -P "$parent" >/dev/null 2>&1 && pwd)" || return 1
    printf '%s/%s\n' "$parent_real" "$base"
    return
  fi
  parent="$(dirname "$path")"
  base="$(basename "$path")"
  parent_real="$(cd -P "$parent" >/dev/null 2>&1 && pwd)" || return 1
  printf '%s/%s\n' "$parent_real" "$base"
}

clean_candidate_path() {
  local path
  path="$1"
  path="${path%%	*}"
  case "$path" in
    \"*\") path="${path#\"}"; path="${path%\"}" ;;
  esac
  case "$path" in
    a/*|b/*) path="${path#?/}" ;;
  esac
  [ "$path" = "/dev/null" ] && return 1
  printf '%s\n' "$path"
}

mark_if_code_file() {
  local raw path abs base
  raw="$1"
  path="$(clean_candidate_path "$raw")" || return 0
  abs="$(normalize_abs_path "$path")" || return 0

  # 只管项目目录内的文件，/tmp 等外部路径不触发
  case "$abs" in
    "$ROOT_REAL"/*) ;;
    *) return 0 ;;
  esac

  base="$(basename "$abs")"

  # 排除框架元目录；这些改动属于能力包自身，不触发项目代码 review gate。
  case "$abs" in
    */.claude/*|*/.codex/*|*/.agents/*)
      return 0
      ;;
  esac

  # 重要工程入口和构建配置即使无扩展名或是 json/yaml/toml，也要触发 review。
  case "$base" in
    Dockerfile|Dockerfile.*|Makefile|makefile|GNUmakefile|Procfile|Brewfile|Gemfile|Rakefile|Jenkinsfile|Justfile|Taskfile.yml|Taskfile.yaml| \
    package.json|tsconfig.json|tsconfig.*.json|jsconfig.json|jsconfig.*.json|turbo.json|pnpm-workspace.yaml|pnpm-workspace.yml| \
    docker-compose.yml|docker-compose.yaml|docker-compose.*.yml|docker-compose.*.yaml|compose.yml|compose.yaml|compose.*.yml|compose.*.yaml| \
    pyproject.toml|Cargo.toml|go.mod|go.sum|pom.xml|build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts| \
    next.config.*|nuxt.config.*|vite.config.*|vitest.config.*|jest.config.*|playwright.config.*|webpack.config.*|rollup.config.*| \
    tailwind.config.*|postcss.config.*|eslint.config.*|prettier.config.*)
      mkdir -p "$(dirname "$STATE_FILE")"
      echo "needs_review" > "$STATE_FILE"
      return 0
      ;;
  esac

  # 无扩展名的文件（脚本草稿、数据、内容稿等）默认不是项目代码，不触发。
  case "$base" in
    *.*) ;;
    *) return 0 ;;
  esac

  # 排除常见非代码文件，其余扩展名文件标记需要 review。
  case "$abs" in
    *.md|*.txt|*.lock|*.log|*.env|*.env.*|*.gitignore|*.prettierrc|*.eslintrc|*.json|*.yaml|*.yml|*.toml)
      return 0
      ;;
  esac

  mkdir -p "$(dirname "$STATE_FILE")"
  echo "needs_review" > "$STATE_FILE"
}

extract_patch_paths() {
  awk '
    /^\*\*\* (Add|Update|Delete) File: / {
      sub(/^\*\*\* (Add|Update|Delete) File: /, "")
      print
      next
    }
    /^\*\*\* Move to: / {
      sub(/^\*\*\* Move to: /, "")
      print
      next
    }
    /^diff --git / {
      line = $0
      sub(/^diff --git /, "", line)
      idx = index(line, " b/")
      if (substr(line, 1, 2) == "a/" && idx > 0) {
        print substr(line, 1, idx - 1)
        print substr(line, idx + 1)
      } else {
        if ($3 != "") print $3
        if ($4 != "") print $4
      }
      next
    }
    /^--- / || /^\+\+\+ / {
      line = $0
      sub(/^(---|\+\+\+) /, "", line)
      if (line != "") print line
      next
    }
  '
}

if command -v jq >/dev/null 2>&1; then
  printf '%s' "$INPUT" | jq -r '
    [
      .tool_input.file_path?,
      .tool_input.path?,
      .tool_input.filename?,
      (.tool_input.files[]? | if type == "string" then . else (.file_path? // .path?) end)
    ] | .[]? // empty
  ' 2>/dev/null | while IFS= read -r path; do
    mark_if_code_file "$path"
  done

  printf '%s' "$INPUT" | jq -r '
    [
      .tool_input.patch?,
      .tool_input.input?,
      .tool_input.command?,
      .patch?,
      .input?
    ] | .[]? | select(type == "string")
  ' 2>/dev/null | extract_patch_paths | while IFS= read -r path; do
    mark_if_code_file "$path"
  done
fi

exit 0
