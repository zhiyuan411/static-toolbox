# Static Toolbox

Static Toolbox 是一个轻量级的前端工具集合，提供了常用的编码转换、格式化和压缩工具等。本项目采用纯静态页面实现，无需后端服务，可直接部署在任何静态文件服务器上。无需任何依赖或构建过程，所有工具均可直接在浏览器中打开使用，支持即时操作和结果展示。

## 核心特性

- 零配置 - 无需安装、无需构建工具
- 无需后端 - 可直接部署在任何静态文件服务器上
- 直接运行 - 所有工具直接通过浏览器打开 HTML 文件即可使用
- 轻量级 - 每个工具独立封装，体积小巧
- 隐私保护 - 所有数据处理在浏览器本地完成，不上传至服务器
- 响应式设计 - 适配各种屏幕尺寸的设备

## 工具列表

Static Toolbox 目前提供以下几类工具，并将持续扩展：

| 工具类别 | 功能描述 | 文件路径 |
|----------|----------|----------|
| **Base64** | Base64等 编解码工具，支持文本/图片互转 | [base64/index.html](base64/) |
| **代码格式化** | 格式化美化 JavaScript/HTML/CSS/JSON 等代码 | [code-formatting/index.html](code-formatting/) |
| **编码转换** | 支持 URL、HTML 实体、Unicode 等多种编码转换 | [encoding/index.html](encoding/) |
| **哈希计算** | 生成 MD5、SHA-1、SHA-256 等哈希值 | [hash/index.html](hash/) |
| **ZIP+Base64** | ZIP等 压缩并转换为 Base64 字符串 | [zip-base64/index.html](zip-base64/) |


## 安装与部署

### 方法一：直接部署静态文件

1. 克隆项目到本地：
   ```bash
   git clone https://github.com/zhiyuan411/static-toolbox.git
   ```

2. 将项目文件复制到静态文件服务器的根目录或指定目录。

3. 确保服务器已正确配置 MIME 类型，特别是 HTML、CSS、JavaScript 和图片文件。

### 方法二：使用 Nginx 部署

1. 安装 Nginx：
   ```bash
   # Ubuntu/Debian
   sudo apt-get install nginx

   # CentOS/RHEL
   sudo yum install nginx
   ```

2. 配置 Nginx：
   ```nginx
   server {
       listen 80;
       server_name your-domain.com;  # 替换为您的域名

       root /path/to/static-toolbox;  # 替换为项目路径
       index index.html;

       location / {
           try_files $uri $uri/ /index.html;
       }
   }
   ```

3. 重启 Nginx：
   ```bash
   sudo systemctl restart nginx
   ```

### 方法三：使用 Docker 部署

1. 创建 Dockerfile：
   ```dockerfile
   FROM nginx:alpine

   # 删除默认的 Nginx 配置
   RUN rm -rf /etc/nginx/conf.d/*

   # 添加自定义配置
   COPY nginx.conf /etc/nginx/conf.d/default.conf

   # 复制项目文件
   COPY . /usr/share/nginx/html

   EXPOSE 80

   CMD ["nginx", "-g", "daemon off;"]
   ```

2. 创建 Nginx 配置文件（nginx.conf）：
   ```nginx
   server {
       listen 80;
       server_name _;

       root /usr/share/nginx/html;
       index index.html;

       location / {
           try_files $uri $uri/ /index.html;
       }
   }
   ```

3. 构建 Docker 镜像：
   ```bash
   docker build -t static-toolbox .
   ```

4. 运行容器：
   ```bash
   docker run -d -p 80:80 static-toolbox
   ```

## 使用方法

1. 访问部署后的网站（通常是 `http://your-domain.com`）。

2. 在首页链接末尾添加具体的工具路径，进入相应的工具列表页面。

3. 选择具体工具后，按照页面提示输入数据并执行相应操作。

4. 对于支持文件上传的工具，点击上传按钮选择本地文件。

## 开发与贡献

### 开发环境搭建

1. 克隆项目：
   ```bash
   git clone https://github.com/zhiyuan411/static-toolbox.git
   cd static-toolbox
   ```

2. 启动本地开发服务器（可使用任何静态文件服务器，如 Python 的 SimpleHTTPServer）：
   ```bash
   python3 -m http.server 8000
   ```

3. 访问 `http://localhost:8000` 查看项目。

### 如何添加新工具

1. **创建工具文件**：
   - 创建合适的子目录（如 `encoding/`、`formatting/`）
   - 在子目录中创建新的 HTML 文件（如 `new-tool.html`）

2. **编写工具代码**：
   - 在页面中编写独立脚本
   - 确保工具界面简洁易用，功能完整

### 贡献流程

1. Fork 本仓库
2. 创建您的功能分支：`git checkout -b feature/your-feature`
3. 提交您的更改：`git commit -m 'Add some feature'`
4. 将您的分支推送到远程：`git push origin feature/your-feature`
5. 提交 Pull Request

## 许可证

本项目采用 [MIT 许可证](https://opensource.org/licenses/MIT)。

## 联系我们

如果您有任何问题或建议，请通过以下方式联系我们：

- GitHub Issues：[https://github.com/zhiyuan411/static-toolbox/issues](https://github.com/zhiyuan411/static-toolbox/issues)
- Email：zhiyuan411@gmail.com

