"""腾讯云 COS 配置 — 请填写你的凭证"""

import os

COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "")
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "")

# Bucket 信息
COS_BUCKET = "nickhome-1329273633"        # 例如: nick-1234567890
COS_REGION = "ap-seoul"        # 例如: ap-guangzhou
COS_PREFIX = "h5-chat/"  # 上传文件的前缀路径，可按需修改

# 可选：自定义域名（已绑定 CDN/CNAME 的域名）
COS_CUSTOM_DOMAIN = "https://nickstorage.top"  # 例如: https://cos.yourdomain.com
