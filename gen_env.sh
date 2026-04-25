#!/bin/sh

# 定义 .env 文件的固定路径（可根据需要修改）
ENV_PATH="./.env"

# 删除原有 .env 文件（如果存在）
if [ -f "$ENV_PATH" ]; then
    echo "发现原有 .env 文件，正在删除..."
    rm "$ENV_PATH"
    if [ $? -eq 0 ]; then
        echo "删除成功。"
    else
        echo "删除失败，请检查权限。" >&2
        exit 1
    fi
else
    echo "未找到原有 .env 文件，将直接创建新文件。"
fi

# 交互式输入 token
echo "请输入新的 NODE_TOKEN："
read -r NODE_TOKEN

# 检查输入是否为空
if [ -z "$NODE_TOKEN" ]; then
    echo "错误：TOKEN 不能为空！" >&2
    exit 1
fi

# 写入新的 .env 文件
echo "NODE_TOKEN=$NODE_TOKEN" > "$ENV_PATH"
if [ $? -eq 0 ]; then
    echo "成功生成 .env 文件到路径：$ENV_PATH"
    echo "内容为：NODE_TOKEN=$NODE_TOKEN"
else
    echo "写入失败，请检查路径权限。" >&2
    exit 1
fi
