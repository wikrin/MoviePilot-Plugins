#!/bin/bash

# 每行格式：前端目录|后端目录
plugin_mappings=(
  "frontend.v2|plugins.v2"
  "frontend.v3|plugins.v3"
)

# 未识别版本时按 v2 处理
IFS='|' read -r frontend_dir backend_dir <<< "${plugin_mappings[0]}"

# 白名单（支持通配符）
keep_patterns=(
  "__federation_*.js"
  "__federation_*.css"
  "_plugin-vue_export-helper-*.js"
  "remoteEntry.js"
)

# 黑名单（明确删除的目录或文件）
blacklist=(
  "__federation_shared_vuetify"
  "*.log"
  "*.tmp"
)

# 获取运行模式（dev 或 build，默认为 build）
run_mode="${1:-build}"

# 接收路径（从参数获取，参数为空则使用环境变量）
current_path="${2:-$PLUGIN_PATH}"

# 当前工作目录
ROOT_DIR=$(pwd)

# 运行模式
echo "⚙️ Run mode: $run_mode"

# 兼容 Windows
current_path="${current_path//\\//}"

# 去除盘符（Windows 和 WSL）
case "$current_path" in
  [Aa]:/*|[Bb]:/*|[Cc]:/*|[Dd]:/*|[Ee]:/*|[Ff]:/*|[Gg]:/*)
    current_path="/${current_path:2}" ;;
  /mnt/[a-zA-Z]/*)
    current_path="/${current_path:5}" ;;
esac

# 清理开头和结尾的 /
current_path="${current_path#/}"
current_path="${current_path%/}"

# 拆分路径数组
parts=()
tmp=""

for ((i = 0; i < ${#current_path}; i++)); do
  c="${current_path:i:1}"
  if [[ "$c" == "/" ]]; then
    if [[ -n "$tmp" ]]; then
      parts+=("$tmp")
      tmp=""
    fi
  else
    tmp+="$c"
  fi
done

if [[ -n "$tmp" ]]; then
  parts+=("$tmp")
fi

# 从路径中的目录名识别插件版本和插件名
target_index=-1
for i in "${!parts[@]}"; do
  path_part="${parts[$i]}"
  for mapping in "${plugin_mappings[@]}"; do
    IFS='|' read -r mapped_frontend_dir mapped_backend_dir <<< "$mapping"
    if [[ "$path_part" == "$mapped_frontend_dir" || "$path_part" == "$mapped_backend_dir" ]]; then
      frontend_dir="$mapped_frontend_dir"
      backend_dir="$mapped_backend_dir"
      target_index=$i
      break
    fi
  done
  [[ $target_index -ne -1 ]] && break
done

plugin_name=""
if [[ $target_index -ne -1 && $((target_index + 1)) -lt ${#parts[@]} ]]; then
  plugin_name="${parts[$((target_index + 1))]}"
elif [[ " ${parts[*]} " =~ " src " ]]; then
  for i in "${!parts[@]}"; do
    if [[ "${parts[$i]}" == "src" && $i -gt 0 ]]; then
      plugin_name="${parts[$((i - 1))]}"
      break
    fi
  done
fi

if [[ -z "$plugin_name" ]]; then
  plugin_name=$(basename "$current_path")
fi

frontend_path="$ROOT_DIR/$frontend_dir/$plugin_name"
echo "📦 Plugin directories: $frontend_dir/$plugin_name -> $backend_dir/$plugin_name"
cd "$frontend_path" || { echo "🚫 Directory not found: $frontend_path"; exit 1; }

# 安装依赖
if [ ! -d "node_modules" ]; then
  echo "📦 Installing dependencies..."
  yarn install || { echo "❌ Failed to install dependencies"; exit 1; }
fi

# 根据模式执行不同命令
case "$run_mode" in
  dev|development)
    echo "🚀 Starting development: $plugin_name"
    yarn dev --config "vite.config.js" || { echo "❌ Development failed"; exit 1; }
    ;;

  build|prod|production)
    # 执行构建
    echo "🏗️ Building: $plugin_name"
    yarn build --config "vite.config.js" || { echo "❌ Build failed"; exit 1; }

    target_dir="$ROOT_DIR/$backend_dir/$plugin_name"
    mkdir -p "$target_dir"

    # 清理旧 dist 目录（如果存在）
    if [ -d "$target_dir/dist" ]; then
      echo "🧹 Cleaning up existing dist directory..."
      rm -rf "$target_dir/dist" || { echo "❌ Failed to remove old dist directory"; exit 1; }
    fi

    # 移动 dist 目录
    mv dist "$target_dir/dist" || { echo "❌ Failed to move dist directory"; exit 1; }

    echo "✅ Build artifacts moved to: $target_dir/dist"

    # 清理
    dist_dir="$target_dir/dist"

    if [ -z "$dist_dir" ]; then
      echo "⚠️ Usage: $0 <dist_directory>"
      exit 1
    fi

    if [ ! -d "$dist_dir" ]; then
      echo "❌ Directory not found: $dist_dir"
      exit 1
    fi

    echo "🧼 Cleaning up dist directory: $dist_dir"

    cd "$dist_dir" || exit 1

    # 判断是否在白名单中
    function is_kept_file() {
      local file="$1"
      for pattern in "${keep_patterns[@]}"; do
        if [[ "$file" == $pattern ]]; then
          return 0
        fi
      done
      return 1
    }

    # 判断是否在黑名单中
    function is_blacklisted_dir() {
      local dir="$1"
      for pattern in "${blacklist[@]}"; do
        if [[ "$dir" == $pattern ]]; then
          return 0
        fi
      done
      return 1
    }

    # 遍历并删除不在白名单的文件
    find . -type f | while read -r file; do
      filename=$(basename "$file")
      relpath="${file:2}"  # 去掉 "./"
      if ! is_kept_file "$filename"; then
        echo "🗑️ Removing file: $relpath"
        rm -f "$relpath"
      fi
    done

    # 遍历并删除黑名单目录
    find . -type d | while read -r dir; do
      dirname=$(basename "$dir")
      relpath="${dir:2}"
      if [ "$dirname" != "." ] && is_blacklisted_dir "$dirname"; then
        echo "💣 Removing directory: $relpath"
        rm -rf "$relpath"
      fi
    done

    # 删除所有空目录
    find . -type d -empty -not -path "." | while read -r dir; do
      relpath="${dir:2}"
      echo "🧱 Removing empty directory: $relpath"
      rmdir "$relpath" 2>/dev/null || true
    done

    echo "🎉 ✅ Cleanup completed."
    ;;

  *)
    echo "❌ Invalid run mode: $run_mode"
    echo "💡 Usage: $0 [build|dev]"
    exit 1
    ;;
esac