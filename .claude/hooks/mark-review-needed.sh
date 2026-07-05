#!/bin/bash
# PostToolUse hook: 代码文件被编辑/创建后标记需要 review
# 排除已知的非代码文件，其余都触发

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
STATE_FILE="$CLAUDE_PROJECT_DIR/.agents/.needs-review"

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# 只管项目目录内的文件，/tmp 等外部路径不触发
case "$FILE_PATH" in
  "$CLAUDE_PROJECT_DIR"/*) ;;
  *) exit 0 ;;
esac

BASE_NAME="$(basename "$FILE_PATH")"

# 排除框架元目录；这些改动属于能力包自身，不触发项目代码 review gate。
case "$FILE_PATH" in
  */.claude/*|*/.codex/*|*/.agents/*)
    exit 0
    ;;
esac

# 重要工程入口和构建配置即使无扩展名或是 json/yaml/toml，也要触发 review。
case "$BASE_NAME" in
  Dockerfile|Dockerfile.*|Makefile|makefile|GNUmakefile|Procfile|Brewfile|Gemfile|Rakefile|Jenkinsfile|Justfile|Taskfile.yml|Taskfile.yaml| \
  package.json|tsconfig.json|tsconfig.*.json|jsconfig.json|jsconfig.*.json|turbo.json|pnpm-workspace.yaml|pnpm-workspace.yml| \
  docker-compose.yml|docker-compose.yaml|docker-compose.*.yml|docker-compose.*.yaml|compose.yml|compose.yaml|compose.*.yml|compose.*.yaml| \
  pyproject.toml|Cargo.toml|go.mod|go.sum|pom.xml|build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts| \
  next.config.*|nuxt.config.*|vite.config.*|vitest.config.*|jest.config.*|playwright.config.*|webpack.config.*|rollup.config.*| \
  tailwind.config.*|postcss.config.*|eslint.config.*|prettier.config.*)
    mkdir -p "$(dirname "$STATE_FILE")"
    echo "needs_review" > "$STATE_FILE"
    exit 0
    ;;
esac

# 无扩展名的文件（脚本草稿、数据、内容稿等）默认不是项目代码，不触发。
case "$BASE_NAME" in
  *.*) ;;
  *) exit 0 ;;
esac

# 排除常见非代码文件，其余扩展名文件标记需要 review。
case "$FILE_PATH" in
  *.md|*.txt|*.lock|*.log|*.env|*.env.*|*.gitignore|*.prettierrc|*.eslintrc|*.json|*.yaml|*.yml|*.toml)
    exit 0
    ;;
esac

mkdir -p "$(dirname "$STATE_FILE")"
echo "needs_review" > "$STATE_FILE"

exit 0
