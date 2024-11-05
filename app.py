import os
import base64
import uuid
import requests
from flask import Flask, request, jsonify, send_file, abort, render_template_string, current_app
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache  # 新增导入
from flask import g
import mimetypes
import magic
import logging
# 配置日志
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# 配置部分
GITHUB_USERNAME = os.getenv('GITHUB_USERNAME', 'default_username')  # 替换为您的 GitHub 用户名
GITHUB_REPO = os.getenv('GITHUB_REPO', 'default_repo')  # 替换为您的仓库名称
GITHUB_BRANCH = os.getenv('GITHUB_BRANCH', 'main')  # 默认分支，通常为 main 或 master
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', 'default_token')  # 替换为您的 GitHub Personal Access Token
RELAY_URL = os.getenv('RELAY_URL', 'https://default_relay_url.com')  # 替换为您的中转程序 URL
SECRET_TOKEN = os.getenv('SECRET_TOKEN', 'default_secret_token') # 定义一个随机密钥仅用于手动清理
FILE_RETENTION_DAYS = int(os.getenv('FILE_RETENTION_DAYS', 1))  # 定义文件的过期时间用于自动清理
RELAY_MODEL = os.getenv('RELAY_MODEL', 'fileupload') # 自定义模型名,用于使用中转key鉴权

# GitHub API 基础 URL
GITHUB_API_URL = 'https://api.github.com'

# 文件大小限制（字节）
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# 设置 Flask 的最大请求内容长度为6MB（5MB + 1MB）
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE + 1 * 1024 * 1024  # 6MB

# Flask-Caching 配置
cache_config = {
    "CACHE_TYPE": "SimpleCache",  # 简单内存缓存，适用于单实例应用。如需分布式缓存，可使用 Redis 等
    "CACHE_DEFAULT_TIMEOUT": 300  # 默认缓存超时时间为300秒（5分钟）
}
app.config.from_mapping(cache_config)
cache = Cache(app)


# 前端上传页面模板
HTML_TEMPLATE = """ 
<!DOCTYPE html> 
<html lang="zh-CN"> 
<head> 
    <meta charset="UTF-8"> 
    <title>文件上传</title> 
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"> 
    <style> 
        body { 
            background-color: #f8f9fa; 
        } 
        .upload-container { 
            max-width: 500px; 
            margin: 50px auto; 
            padding: 30px; 
            background-color: #ffffff; 
            border-radius: 10px; 
            box-shadow: 0 0 10px rgba(0,0,0,0.1); 
        } 
        .upload-container h2 { 
            margin-bottom: 20px; 
        } 
    </style> 
</head> 
<body> 
    <div class="upload-container"> 
        <h2 class="text-center">文件上传</h2> 
        <form id="upload-form" enctype="multipart/form-data"> 
            <div class="mb-3"> 
                <label for="auth_type" class="form-label">认证方式</label> 
                <select class="form-select" id="auth_type" name="auth_type" required> 
                    <option value="custom_model">token(请求相应模型进行验证，可能会扣费)</option> 
                    <option value="accesstoken">AccessToken(有频率限制,不会进行扣费)</option> 
                </select> 
            </div> 
            <div class="mb-3"> 
                <label for="token" class="form-label">用户 Token</label> 
                <input type="text" class="form-control" id="token" name="token" placeholder="请输入您的 Token" required> 
            </div> 
            <div class="mb-3"> 
                <label for="file" class="form-label">选择文件</label> 
                <input class="form-control" type="file" id="file" name="file" required> 
                <div class="form-text">最大允许上传5MB的文件。</div> 
            </div> 
            <button type="submit" class="btn btn-primary w-100" id="upload-button">上传</button> 
        </form> 
        <div id="loading-message" class="mt-3" style="display: none;">
            <div class="alert alert-info" role="alert">正在上传中，请稍候...</div>
        </div>
        <div id="response-message" class="mt-3"></div>
        <div id="file-link" class="mt-2"></div>
   </div> 

    <script> 
        document.getElementById('upload-form').addEventListener('submit', async function(event) { 
            event.preventDefault(); // 阻止默认表单提交行为 

            const form = event.target; 
            const formData = new FormData(form); 

            // Clear previous messages 
            const messageDiv = document.getElementById('response-message'); 
            const fileLinkDiv = document.getElementById('file-link'); 
            messageDiv.innerHTML = ''; 
            fileLinkDiv.innerHTML = ''; 
            const uploadButton = document.getElementById('upload-button');
            uploadButton.disabled = true;
            
            const loadingMessageDiv = document.getElementById('loading-message');
            loadingMessageDiv.style.display = 'block';

            try { 
                const response = await fetch('/upload', { 
                    method: 'POST', 
                    body: formData, 
                    headers: { 
                        'Accept': 'application/json' 
                    } 
                }); 

                const result = await response.json(); 

                if (response.ok) { 
                    if (result.success) { 
                        messageDiv.innerHTML = `<div class="alert alert-success" role="alert">${result.message}</div>`; 
                        if (result.file_url) { 
                            fileLinkDiv.innerHTML = `
                                <a href="${result.file_url}" class="btn btn-success">访问文件</a>
                                <p class="mt-2">文件链接: <a href="${result.file_url}" target="_blank">${result.file_url}</a></p>
                            `; 
                        } 
                    } else { 
                        messageDiv.innerHTML = `<div class="alert alert-danger" role="alert">${result.message}</div>`; 
                    } 
                } else { 
                    messageDiv.innerHTML = `<div class="alert alert-danger" role="alert">${result.message || '上传失败，请稍后再试。'}</div>`; 
                } 
            } catch (error) { 
                console.error('Error:', error); 
                messageDiv.innerHTML = `<div class="alert alert-danger" role="alert">上传过程中发生错误，请稍后再试。</div>`; 
            } finally {
                uploadButton.disabled = false;
          	    loadingMessage.style.display='none';
          	}
        }); 
    </script> 
</body> 
</html> 
"""

# 手动清理页面模板（调整为同时支持按日期删除和按数量删除）
CLEANUP_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>手动清理</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            background-color: #f8f9fa;
        }
        .cleanup-container {
            max-width: 600px;
            margin: 50px auto;
            padding: 30px;
            background-color: #ffffff;
            border-radius: 10px;
            box-shadow: 0 0 10px rgba(0,0,0,0.1);
        }
        .cleanup-container h2 {
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="cleanup-container">
        <h2 class="text-center">手动清理仓库</h2>
        <form method="POST" action="{{ url_for('manual_cleanup') }}">
            <div class="mb-3">
                <label class="form-label">选择清理方式</label>
                <div>
                    <div class="form-check form-check-inline">
                        <input class="form-check-input" type="radio" name="cleanup_type" id="by_days" value="days" checked>
                        <label class="form-check-label" for="by_days">按天数删除</label>
                    </div>
                    <div class="form-check form-check-inline">
                        <input class="form-check-input" type="radio" name="cleanup_type" id="by_count" value="count">
                        <label class="form-check-label" for="by_count">按数量删除</label>
                    </div>
                </div>
            </div>
            <div class="mb-3" id="days_input">
                <label for="days" class="form-label">保留天数</label>
                <input type="number" class="form-control" id="days" name="days" min="0" placeholder="请输入保留的天数" required>
                <div class="form-text">删除超过此天数的文件。</div>
            </div>
            <div class="mb-3 d-none" id="count_input">
                <label for="delete_count" class="form-label">要删除的文件数量</label>
                <input type="number" class="form-control" id="delete_count" name="delete_count" min="1" placeholder="请输入要删除的文件数量">
                <div class="form-text">删除最旧的X个文件。</div>
            </div>
            <button type="submit" class="btn btn-primary w-100">开始清理</button>
        </form>
        {% if message %}
            <div class="mt-3 alert alert-{{ 'success' if success else 'danger' }}" role="alert">
                {{ message }}
            </div>
        {% endif %}
        <div class="mt-3 text-center">
            <a href="{{ url_for('index') }}" class="btn btn-secondary">返回主页</a>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const byDaysRadio = document.getElementById('by_days');
            const byCountRadio = document.getElementById('by_count');
            const daysInput = document.getElementById('days_input');
            const countInput = document.getElementById('count_input');

            byDaysRadio.addEventListener('change', function() {
                if (byDaysRadio.checked) {
                    daysInput.classList.remove('d-none');
                    countInput.classList.add('d-none');
                    document.getElementById('days').required = true;
                    document.getElementById('delete_count').required = false;
                }
            });

            byCountRadio.addEventListener('change', function() {
                if (byCountRadio.checked) {
                    countInput.classList.remove('d-none');
                    daysInput.classList.add('d-none');
                    document.getElementById('delete_count').required = true;
                    document.getElementById('days').required = false;
                }
            });
        });
    </script>
</body>
</html>
"""

def upload_to_github(file_content, file_path, commit_message="Add file"):
    """
    将文件上传到 GitHub 仓库。
    """
    url = f"{GITHUB_API_URL}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{file_path}"
    # 编码文件内容为 base64
    content_b64 = base64.b64encode(file_content).decode('utf-8')
    data = {
        "message": commit_message,
        "content": content_b64,
        "branch": GITHUB_BRANCH
    }
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    response = requests.put(url, json=data, headers=headers)
    if response.status_code in [201, 200]:
        return True, response.json()
    elif response.status_code == 403 and 'rate limit' in response.text.lower():
        return False, "服务器当前处理请求过多，请稍后再试。"
    else:
        return False, "文件上传失败，请稍后再试。"


def authenticate_user():
    """
    认证用户并存储结果在请求上下文中
    """
    if hasattr(g, 'auth_result'):
        return g.auth_result

    token = request.form.get('token')
    auth_type = request.form.get('auth_type')

    if not token or not auth_type:
        return False, "缺少用户 Token 或认证方式。"

    auth_success, user_info = get_user_info(token, auth_type)
    g.auth_result = (auth_success, user_info)
    return g.auth_result


def get_user_info(token, auth_type):
    """
    根据选择的认证方式获取用户信息。
    """
    if auth_type == 'accesstoken':
        return get_user_info_accesstoken(token)
    elif auth_type == 'custom_model':
        return authenticate_with_custom_model(token)
    else:
        return False, "无效的认证方式"

@cache.memoize(timeout=300)  # 缓存 5 分钟
def get_user_info_accesstoken(token):
    """
    通过accesstoken获取用户信息。
    """
    url = f"{RELAY_URL}/api/user/self"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, "用户认证失败或Token无效。"
    except requests.RequestException:
        return False, "无法连接到认证服务器，请稍后再试。"

def authenticate_with_custom_model(token):
    """
    使用自定义模型进行认证
    """
    url = f"{RELAY_URL}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    data = {
        "model": RELAY_MODEL,
        "messages": [{"role": "user", "content": "echo hi"}],
        "stream": False,
        "max_tokens": 1
    }
    try:
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 200:
            return True, {"data": {"quota": 10000000000, "used_quota": 0}}  # 假设配额
        else:
            return False, "用户认证失败或Token无效。"
    except requests.RequestException:
        return False, "无法连接到认证服务器,请稍后再试。"

@app.route('/', methods=['GET'])
def index():
    """
    渲染上传页面。
    """
    return render_template_string(HTML_TEMPLATE)

# 初始化 Flask-Limiter
limiter = Limiter(
    key_func=lambda: request.view_args.get('token') or get_remote_address(),
    app=app,
    default_limits=[]
)

def rate_limit():
    """
    动态计算每分钟的请求限制
    """
    auth_success, user_info = authenticate_user()
    if not auth_success:
        return "1 per minute"

    try:
        quota = user_info['data']['quota']
        used_quota = user_info['data']['used_quota']
        total_quota = quota + used_quota
        rate = max(1, int(total_quota / 5000000))
    except (KeyError, TypeError):
        rate = 1

    return f"{rate} per minute"

@app.route('/upload', methods=['POST']) 
@limiter.limit(rate_limit) 
def upload_file(): 
    """ 
    处理文件上传请求。 
    """ 
    auth_success, user_info = authenticate_user() 
    if not auth_success: 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": user_info}), 401
        else:
            return render_template_string(HTML_TEMPLATE, message=user_info, success=False) 

    # 提取 quota 和 used_quota 
    try: 
        quota = user_info['data']['quota'] 
        used_quota = user_info['data']['used_quota'] 
    except (KeyError, TypeError): 
        message = "用户信息格式错误。" 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 500
        else:
            return render_template_string(HTML_TEMPLATE, message=message, success=False) 

    # 检查 quota 和 used_quota 
    if (quota + used_quota) <= 2500000: 
        message = "您的配额不足，无法上传文件。" 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 400
        else:
            return render_template_string(HTML_TEMPLATE, message=message, success=False) 

    # 检查文件是否存在 
    if 'file' not in request.files: 
        message = "请求中没有文件部分。" 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 400
        else:
            return render_template_string(HTML_TEMPLATE, message=message, success=False) 
    file = request.files['file'] 
    if file.filename == '': 
        message = "没有选择要上传的文件。" 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 400
        else:
            return render_template_string(HTML_TEMPLATE, message=message, success=False) 

    # 检查文件大小 
    file.seek(0, os.SEEK_END) 
    file_length = file.tell() 
    file.seek(0)  # 重置文件指针 
    if file_length > MAX_FILE_SIZE: 
        message = f"上传的文件过大，最大允许{MAX_FILE_SIZE/1048576}MB。" 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 413
        else:
            return render_template_string(HTML_TEMPLATE, message=message, success=False) 

    # 读取文件内容 
    file_content = file.read() 
    # 生成随机文件名，保留原扩展名 
    ext = os.path.splitext(file.filename)[1] 
    random_filename = f"{uuid.uuid4().hex}{ext}" 
    # 上传到 GitHub 
    upload_success, result = upload_to_github(file_content, random_filename) 
    if upload_success: 
        file_url = f"{request.host_url}file/{random_filename}" 
        message = "文件上传成功！" 
        success = True 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": True, "message": message, "file_url": file_url}), 200
    else: 
        message = result  # 包含错误信息 
        if request.headers.get('Accept') == 'application/json':
            return jsonify({"success": False, "message": message}), 500

    return render_template_string(HTML_TEMPLATE, message=message, success=success, file_url=file_url)

def clean_github_repository(retention_days=None, number_to_delete=None):
    """
    清理 GitHub 仓库中超过保留天数的文件或指定数量的最旧文件。

    Args:
        retention_days (int, optional): 文件保留的天数。
        number_to_delete (int, optional): 要删除的文件数量。
    """
    if retention_days is not None:
        print(f"开始按天数清理，保留天数: {retention_days}")
    elif number_to_delete is not None:
        print(f"开始按数量清理，准备删除文件数量: {number_to_delete}")
    else:
        print("无有效的清理参数。")
        return "无有效的清理参数。"

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    # 获取仓库的树（包括所有文件）
    tree_url = f"{GITHUB_API_URL}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
    response = requests.get(tree_url, headers=headers)
    if response.status_code != 200:
        print(f"无法获取仓库树: {response.status_code} - {response.text}")
        return f"无法获取仓库树: {response.status_code}"

    tree = response.json().get('tree', [])
    files = [f for f in tree if f['type'] == 'blob']  # 只处理文件

    files_with_dates = []
    for file in files:
        file_path = file['path']
        # 获取文件的最新提交
        commits_url = f"{GITHUB_API_URL}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/commits"
        params = {
            "path": file_path,
            "sha": GITHUB_BRANCH,
            "per_page": 1
        }
        commits_response = requests.get(commits_url, headers=headers, params=params)
        if commits_response.status_code != 200:
            print(f"无法获取文件 {file_path} 的提交信息: {commits_response.status_code} - {commits_response.text}")
            continue

        commits = commits_response.json()
        if not commits:
            print(f"文件 {file_path} 没有找到任何提交记录。")
            continue

        latest_commit = commits[0]
        try:
            commit_date_str = latest_commit['commit']['committer']['date']
        except KeyError:
            # 如果 'committer' 不存在，尝试使用 'author'
            try:
                commit_date_str = latest_commit['commit']['author']['date']
                print(f"文件 {file_path} 的提交中 'committer' 缺失，使用 'author' 日期。")
            except KeyError:
                print(f"文件 {file_path} 的提交信息中既没有 'committer' 也没有 'author'，跳过。")
                continue

        commit_date = datetime.strptime(commit_date_str, "%Y-%m-%dT%H:%M:%SZ")
        files_with_dates.append((file, commit_date))

    # 根据清理方式选择要删除的文件
    files_to_delete = []

    if retention_days is not None:
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        print(f"截止日期: {cutoff_date}")
        for file, commit_date in files_with_dates:
            if commit_date < cutoff_date:
                files_to_delete.append(file)
    elif number_to_delete is not None:
        # 按提交日期排序，最旧的文件在前
        files_sorted_by_date = sorted(files_with_dates, key=lambda x: x[1])
        files_to_delete = [f[0] for f in files_sorted_by_date[:number_to_delete]]

    if not files_to_delete:
        print(f"[{datetime.now()}] 没有需要清理的文件。")
        return "没有需要清理的文件。"

    print(f"需要删除的文件数量: {len(files_to_delete)}")
    deleted_files = []
    for file in files_to_delete:
        file_path = file['path']
        print(f"处理文件: {file_path}")

        # 删除文件
        delete_url = f"{GITHUB_API_URL}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{file_path}"
        delete_message = f"Delete {file_path} as part of cleanup."
        delete_data = {
            "message": delete_message,
            "sha": file['sha'],
            "branch": GITHUB_BRANCH
        }
        delete_response = requests.delete(delete_url, json=delete_data, headers=headers)
        if delete_response.status_code in [200, 202]:
            print(f"成功删除文件: {file_path}")
            deleted_files.append(file_path)
        else:
            print(f"无法删除文件 {file_path}: {delete_response.status_code} - {delete_response.text}")

        # 添加延迟以限制删除速率（可选）
        # time.sleep(0.06)  # 约每分钟1000次删除

    print(f"[{datetime.now()}] 仓库清理完成。成功删除了 {len(deleted_files)} 个文件。")
    return f"仓库清理完成。成功删除了 {len(deleted_files)} 个文件。"


@app.route('/file/<filename>', methods=['GET'])
def get_file(filename):
    """
    通过 GitHub API 获取文件内容并返回给用户。
    适用于私有仓库。
    """
    url = f"{GITHUB_API_URL}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{filename}?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.raw"  # 获取原始文件内容
    }

    # 请求文件内容
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        file_content = response.content

        # 使用 python-magic 推断 MIME 类型
        mime = magic.Magic(mime=True)
        mime_type = mime.from_buffer(file_content)
        if not mime_type:
            # 如果无法推断，使用文件扩展名
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = 'application/octet-stream'  # 默认 MIME 类型

        # 判断是否可以预览的文件类型
        previewable_mime_types = [
            # 文本文件
            'text/plain',
            'text/html',
            'text/css',
            'text/javascript',
            'application/javascript',
            'application/json',
            'application/xml',
            'text/markdown',
            'application/xhtml+xml',
            # 图片文件
            'image/jpeg',
            'image/png',
            'image/gif',
            'image/svg+xml',
            'image/webp',
            'image/bmp',
            'image/tiff',
            'image/x-icon',
            # 文档文件
            'application/pdf',
            'application/msword',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-powerpoint',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            'application/rtf',
            'application/vnd.oasis.opendocument.text',
            'application/vnd.oasis.opendocument.spreadsheet',
            'application/vnd.oasis.opendocument.presentation',
            # 多媒体文件
            'video/mp4',
            'video/webm',
            'video/ogg',
            'video/quicktime',
            'audio/mpeg',
            'audio/ogg',
            'audio/wav',
            'audio/webm',
            'audio/mp4',
            # 其他文件类型
            'application/zip',
            'application/x-rar-compressed',
            'application/x-7z-compressed',
            'application/x-tar',
            'application/gzip',
            'application/vnd.mozilla.xul+xml',
            # 其他可能的预览类型
            'application/x-shockwave-flash',  # 尽管现代浏览器已逐步停止支持
        ]

        is_previewable = mime_type in previewable_mime_types

        # 设置 as_attachment 的值
        as_attachment = not is_previewable

        # 日志记录（可选）
        current_app.logger.info(f"Serving file '{filename}' with MIME type '{mime_type}'. Previewable: {is_previewable}")

        return send_file(
            BytesIO(response.content),
            mimetype=mime_type,
            as_attachment=as_attachment,
            download_name=filename
        )
    else:
        current_app.logger.error(f"File not found: {filename}")
        abort(404)

@app.route(f'/{SECRET_TOKEN}/test_cleanup', methods=['GET', 'POST'])
def manual_cleanup():
    """
    通过测试路由手动执行清理任务，并显示结果。
    路由路径中包含 SECRET_TOKEN 作为安全验证。
    """
    if request.method == 'POST':
        cleanup_type = request.form.get('cleanup_type')
        if cleanup_type == 'days':
            days = request.form.get('days', type=int)
            if days is None or days < 0:
                message = "保留天数必须为非负整数。"
                return render_template_string(CLEANUP_TEMPLATE, message=message, success=False)
            result = clean_github_repository(retention_days=days, number_to_delete=None)
            if "成功删除了" in result:
                success = True
            else:
                success = False
            return render_template_string(CLEANUP_TEMPLATE, message=result, success=success)
        elif cleanup_type == 'count':
            delete_count = request.form.get('delete_count', type=int)
            if delete_count is None or delete_count <= 0:
                message = "要删除的文件数量必须为正整数。"
                return render_template_string(CLEANUP_TEMPLATE, message=message, success=False)
            result = clean_github_repository(retention_days=None, number_to_delete=delete_count)
            if "成功删除了" in result:
                success = True
            else:
                success = False
            return render_template_string(CLEANUP_TEMPLATE, message=result, success=success)
        else:
            message = "无效的清理类型。"
            return render_template_string(CLEANUP_TEMPLATE, message=message, success=False)
    else:
        # 仅渲染清理页面
        return render_template_string(CLEANUP_TEMPLATE)


@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """
    处理中转程序发送的openai格式请求
    """
    return jsonify({
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": RELAY_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "认证成功"
                },
                "logprobs": None,
                "finish_reason": "stop"
            }
        ]
    }), 200

@app.errorhandler(404)
def not_found(e):
    """
    处理404错误。
    """
    return jsonify({"error": "文件未找到"}), 404

@app.errorhandler(413)
def request_entity_too_large(e):
    """
    处理413错误。
    """
    return jsonify({"error": "上传的文件过大，最大允许5MB。"}), 413

@app.errorhandler(500)
def internal_error(e):
    """
    处理500错误。
    """
    return jsonify({"error": "服务器内部错误"}), 500

def start_scheduler():
    """
    启动定时任务调度器。
    """
    scheduler = BackgroundScheduler()
    # 每天凌晨2点执行一次清理任务，删除超过保留天数的文件
    scheduler.add_job(clean_github_repository, 'cron', hour=2, minute=0, args=[FILE_RETENTION_DAYS, None])
    scheduler.start()
    print("定时任务调度器已启动。")

if __name__ == '__main__':
    # 启动定时任务调度器
    start_scheduler()
    app.run(debug=False, host='0.0.0.0', port=5000)
