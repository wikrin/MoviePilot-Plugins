#!/bin/bash

# 插件前端资源存放目录
frontend_dir="frontend"

# 插件后端资源存放目录
backend_dir="plugins.v2"

# 当前工作目录
ROOT_DIR=$(pwd)

# 接收路径（从环境变量中获取）
current_path="$PLUGIN_PATH"

# 获取运行模式（dev 或 build，默认为 build）
run_mode="${1:-build}"

# 原始输入路径
echo "Raw input path: $current_path"
# 运行模式
echo "Run mode: $run_mode"

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

# 匹配目录名
target_index=-1
for i in "${!parts[@]}"; do
if [[ "${parts[$i]}" =~ ^(plugins|plugins\.v2|$frontend_dir)$ ]]; then
    target_index=$i
    break
  fi
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

cd "$frontend_dir/$plugin_name" || { echo "Directory not found: $plugin_name"; exit 1; }

# 安装依赖
if [ ! -d "node_modules" ]; then
  echo "Installing dependencies..."
  yarn install || { echo "Failed to install dependencies"; exit 1; }
fi

# 根据模式执行不同命令
case "$run_mode" in
  dev|development)
    echo "Starting development: $plugin_name"
    yarn dev --config "vite.config.js" || { echo "Development failed"; exit 1; }
    ;;

  build|prod|production)
    # 执行构建
    echo "Building: $plugin_name"
    yarn build --config "vite.config.js" || { echo "Build failed"; exit 1; }

    target_dir="$ROOT_DIR/$backend_dir/$plugin_name"
    mkdir -p "$target_dir"

    # 清理旧 dist 目录（如果存在）
    if [ -d "$target_dir/dist" ]; then
      echo "Cleaning up existing dist directory..."
      rm -rf "$target_dir/dist" || { echo "Failed to remove old dist directory"; exit 1; }
    fi

    # 移动 dist 目录
    mv dist "$target_dir/dist" || { echo "Failed to move dist directory"; exit 1; }

    echo "Build artifacts moved to: $target_dir/dist"
    ;;

  *)
    echo "Invalid run mode: $run_mode"
    echo "Usage: $0 [build|dev]"
    exit 1
    ;;
esac