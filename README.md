# PortaLink

### Turn local, HTTP, or FTP resources into temporary public links.

**PortaLink** is a lightweight Python library that lets you expose a resource through a temporary public URL using a local HTTP server and a **Cloudflare Quick Tunnel**.

It can share resources that are normally reachable only from your own machine or private network, including local files and remote HTTP/HTTPS/FTP resources.

> **Private resource → PortaLink → Local HTTP server → Cloudflare Tunnel → Public URL**

---

## ✨ Features

- 📁 Share a **local file** through a public URL
- 📂 Share a **local directory** as an on-the-fly ZIP archive
- 🌐 Share an **HTTP/HTTPS URL**
- 📡 Share an **FTP resource**
- 🔗 Generate a temporary public URL through Cloudflare Tunnel
- ⏱️ Automatic share expiration
- 🔢 Maximum download limits
- 📊 Download statistics
- 🚀 Streaming downloads instead of loading the entire file into memory
- 📦 HTTP `Range` request support for resumable/partial downloads
- 🔄 Automatic Cloudflare tunnel reconnection
- 🧹 Automatic cleanup of expired and exhausted shares
- 🖥️ Built-in HTTP API
- 📋 Built-in dashboard endpoint
- 🧩 Python API designed to be embedded into other projects
- 🪵 Rotating application logs
- 🔐 Random share IDs
- 🧹 Filename sanitization
- 🪶 Uses Python's standard library for the application itself

---

# 🧠 What is PortaLink?

Suppose you have a file on your computer:

```text
/home/user/Videos/example.mp4
```

Normally, someone outside your network cannot download it directly.

PortaLink creates a local HTTP server and exposes that server through a Cloudflare Quick Tunnel:

```text
                     YOUR DEVICE
┌───────────────────────────────────────────────────────┐
│                                                       │
│  Local File                                            │
│      │                                                │
│      ▼                                                │
│  PortaLink                                            │
│      │                                                │
│      ▼                                                │
│  Local HTTP Server                                     │
│      │                                                │
└──────┼────────────────────────────────────────────────┘
       │
       ▼
  Cloudflare Tunnel
       │
       │ Public HTTPS URL
       ▼
┌───────────────────┐
│   Internet User   │
└───────────────────┘
       │
       ▼
   Download file
```

The original resource does **not need to be uploaded to a cloud-storage service first**.

For remote HTTP/HTTPS and FTP sources, PortaLink can also act as a streaming bridge:

```text
Private HTTP/FTP resource
          │
          ▼
      PortaLink
          │
          ▼
   Public Cloudflare URL
          │
          ▼
      Downloader
```

This makes PortaLink useful when a resource is accessible from your network but not directly accessible from the public internet.

---

# 🚀 Quick Start

## Requirements

PortaLink is designed to require only:

- **Python 3**
- **Cloudflare `cloudflared`**

The Python application itself uses the Python standard library and does not require a large dependency stack.

### Cloudflared

PortaLink can automatically locate `cloudflared` if it is already installed.

If it cannot find it, PortaLink can download an appropriate `cloudflared` binary and cache it under:

```text
~/.Portalink/bin/cloudflared
```

The application therefore normally does **not** require you to manually install `cloudflared`.

> The `cloudflared` binary is third-party software developed by Cloudflare. It is not part of PortaLink's original source code.

---

# 📥 Installation

Clone the repository:

```bash
git clone https://github.com/mrSnow-tr/Portalink.git
cd PortaLink
```

Then use the package directly from the repository, or install/package it according to the project's Python packaging configuration.

For a simple local test:

```bash
python3 test_local.py
```

---

# 📁 Share a Local File

The simplest example:

```python
from PortaLink import ShareManager

file_path = "/path/to/example.zip"

with ShareManager() as manager:
    share = manager.create_share(file_path)

    print("Public URL:", share.public_url)

    input("Press Enter to stop...")
```

Sharelink will:

1. Validate the local path
2. Start its embedded HTTP server
3. Create a Cloudflare tunnel
4. Wait for the public URL
5. Create the share
6. Serve the file through the public URL

Example output:

```text
============================================================
SHARE CREATED
============================================================
Share ID : X8k2Lm91Qp7Za3Rt
Filename : example.zip
Size     : 734003200
State    : ACTIVE
Public URL: https://example-random-name.trycloudflare.com
============================================================
```

---

# 📂 Share a Directory

PortaLink can expose a directory as an automatically generated ZIP archive.

```python
from PortaLink import ShareManager

folder = "/path/to/my_folder"

with ShareManager() as manager:
    share = manager.create_share(folder)

    print("Public URL:", share.public_url)

    input("Press Enter to stop...")
```

The ZIP archive is generated as needed instead of requiring you to create the archive beforehand.

---

# 🌐 Share an HTTP/HTTPS URL

PortaLink can also use an HTTP or HTTPS resource as the source.

Example:

```python
from PortaLink import ShareManager

url = "http://192.168.1.100/files/example.mp4"

with ShareManager() as manager:
    share = manager.create_share(
        url,
        expire_seconds=3600,
        max_downloads=10,
        wait_for_url=True,
        url_timeout=120,
    )

    print("Public URL:", share.public_url)

    input("Press Enter to stop...")
```

This is useful for resources that are accessible from your local/private network but are not directly reachable from the public internet.

For example:

```text
http://192.168.1.100/movie/example.mkv
```

can be exposed through a public Sharelink URL:

```text
https://something-random.trycloudflare.com
```

The remote downloader accesses Sharelink, while PortaLink retrieves the original resource.

---

# 📡 Share an FTP Resource

Share an FTP URL directly:

```python
from PortaLink import ShareManager

ftp_url = "ftp://example.com/path/to/file.zip"

with ShareManager() as manager:
    share = manager.create_share(
        ftp_url,
        expire_seconds=3600,
        max_downloads=10,
    )

    print("Public URL:", share.public_url)

    input("Press Enter to stop...")
```

Anonymous FTP URLs are supported when the server allows anonymous access.

You can also provide FTP credentials separately.

```python
from PortaLink import ShareManager

with ShareManager() as manager:
    share = manager.create_share(
        "/path/to/file.zip",
        ftp_host="192.168.1.20",
        ftp_port=21,
        ftp_username="username",
        ftp_password="password",
    )

    print(share.public_url)

    input("Press Enter to stop...")
```

---

# ⏱️ Expiration

Every share has an expiration time.

Example:

```python
share = manager.create_share(
    "/path/to/file.zip",
    expire_seconds=3600,
)
```

The share expires after:

```text
3600 seconds = 1 hour
```

After expiration, the public download becomes unavailable.

This is useful for temporary sharing because you don't need to manually revoke every link.

---

# 🔢 Download Limits

You can limit how many completed downloads a share can receive.

```python
share = manager.create_share(
    "/path/to/file.zip",
    max_downloads=5,
)
```

After the maximum number of completed downloads has been reached, the share is automatically exhausted.

For example:

```python
expire_seconds=3600
max_downloads=1
```

creates a link that is intended to be available for up to one hour and usable for one completed download.

---

# 🔐 Temporary Sharing Example

For a one-time file transfer:

```python
from PortaLink import ShareManager

with ShareManager() as manager:
    share = manager.create_share(
        "/path/to/private-document.pdf",
        expire_seconds=900,
        max_downloads=1,
        wait_for_url=True,
    )

    print("Send this URL:")
    print(share.public_url)

    input("Press Enter to revoke the share...")
```

This gives you a short-lived sharing link with a single-download limit.

---

# 🧩 Share Object

`create_share()` returns a `Share` object.

Useful properties include:

```python
share.share_id
share.source_path
share.source_type
share.filename
share.file_size
share.created_at
share.expires_at
share.max_downloads
share.content_type
share.state
share.public_url
```

You can also check:

```python
share.is_active()
share.is_expired()
share.is_exhausted()
share.downloads_remaining()
```

---

# 📊 Statistics

PortaLink provides download statistics.

```python
stats = share.statistics()

print(stats)
```

You can also convert a share into a dictionary:

```python
data = share.to_dict()

print(data)
```

This can be useful when integrating PortaLink into another application, dashboard, bot, or automation system.

---

# 🔎 Managing Shares

Create a manager:

```python
from PortaLink import ShareManager

manager = ShareManager()
```

Create a share:

```python
share = manager.create_share("/path/to/file.zip")
```

Find a share:

```python
share = manager.get_share("YOUR_SHARE_ID")
```

List shares:

```python
shares = manager.list_shares()

for share in shares:
    print(share.share_id, share.public_url)
```

Delete/revoke a share:

```python
manager.delete_share("YOUR_SHARE_ID")
```

Clean up expired shares:

```python
manager.cleanup_expired()
```

Clean up finished shares:

```python
manager.cleanup_finished()
```

Shut down Sharelink:

```python
manager.shutdown()
```

---

# ♻️ Recommended Context Manager

The recommended approach is:

```python
from PortaLink import ShareManager

with ShareManager() as manager:
    share = manager.create_share("/path/to/file.zip")

    print(share.public_url)

    input("Press Enter to stop...")
```

When the `with` block ends, Sharelink shuts down active sessions and tunnel processes.

---

# ⚙️ Custom Configuration

PortaLink provides `ShareConfig` for configuring the embedded server, sharing behavior, logging, and tunnel reconnection.

Example:

```python
from PortaLink import ShareManager, ShareConfig

config = ShareConfig(
    port=8080,
    expire_seconds=3600,
    max_downloads=5,
    chunk_size=65536,
    http_timeout=30.0,
    reconnect_delay=5.0,
    reconnect_retries=10,
)

with ShareManager(config=config) as manager:
    share = manager.create_share(
        "/path/to/file.zip"
    )

    print(share.public_url)

    input("Press Enter to stop...")
```

---

# ⚙️ Configuration Options

| Option | Default | Description |
|---|---:|---|
| `host` | `127.0.0.1` | Local HTTP server address |
| `port` | `8080` | Local HTTP server port |
| `expire_seconds` | `86400` | Default share lifetime |
| `max_downloads` | `10` | Default maximum completed downloads |
| `chunk_size` | `65536` | Streaming chunk size |
| `http_timeout` | `30.0` | HTTP connection timeout |
| `session_sweep_interval` | `60` | Cleanup interval |
| `reconnect_delay` | `5.0` | Tunnel reconnect delay |
| `reconnect_retries` | `10` | Maximum reconnect attempts |
| `log_directory` | `~/.sharelink/logs` | Log directory |
| `log_filename` | `sharelink.log` | Log filename |
| `log_backup_count` | `7` | Number of rotated logs retained |
| `cloudflared_binary_path` | `~/.sharelink/bin/cloudflared` | Cloudflared cache location |

Set unlimited tunnel reconnect attempts with:

```python
config = ShareConfig(
    reconnect_retries=-1
)
```

---

# 📡 HTTP Range Requests

PortaLink supports HTTP byte-range requests.

This is important for large files because clients can request only a specific portion of the resource.

For example:

```http
Range: bytes=1000000-1999999
```

This makes PortaLink more suitable for:

- Large downloads
- Download managers
- Resumable downloads
- Media players
- Partial file access
- Clients that request byte ranges

---

# 🎬 Streaming

PortaLink is designed around streaming rather than loading an entire resource into RAM before sending it.

Conceptually:

```text
Source
  │
  ▼
Read small chunk
  │
  ▼
Send chunk
  │
  ▼
Read next chunk
  │
  ▼
Send next chunk
```

The configured default streaming chunk size is:

```text
64 KiB
```

This helps keep memory usage reasonable even when sharing large files.

---

# 🔄 Tunnel Reconnection

PortaLink monitors the `cloudflared` process.

If the tunnel disconnects unexpectedly, PortaLink can attempt to reconnect automatically.

Configure it with:

```python
from PortaLink import ShareConfig

config = ShareConfig(
    reconnect_delay=5.0,
    reconnect_retries=10,
)
```

For unlimited retries:

```python
config = ShareConfig(
    reconnect_retries=-1
)
```

When a new tunnel URL is generated, the PortaLink session updates its public URL accordingly.

---

# 🖥️ Built-in HTTP Server

PortaLink includes an embedded threaded HTTP server.

The server is normally bound to:

```text
127.0.0.1
```

This means the HTTP service itself is not directly exposed to your LAN or internet.

Cloudflare Tunnel forwards public traffic to the local service.

The local server supports:

- `GET`
- `HEAD`
- `DELETE`
- `OPTIONS`

and provides API endpoints for share management and status information.

---

# 🌐 Public URL Architecture

A typical PortaLink session looks like this:

```text
                 INTERNET
                    │
                    │ HTTPS
                    ▼
       ┌─────────────────────────┐
       │   Cloudflare Quick       │
       │        Tunnel            │
       └────────────┬────────────┘
                    │
                    ▼
          127.0.0.1:8080
                    │
          ┌─────────┴─────────┐
          │     PortaLink     │
          └─────────┬─────────┘
                    │
          ┌─────────┼──────────┐
          ▼         ▼          ▼
       Local      HTTP         FTP
        File      Source      Source
```

---

# 📱 Android / Termux

PortaLink is written in Python and can be useful on Linux-based environments such as **Termux**, provided that Python and the required `cloudflared` binary are available for the device architecture.

Example:

```bash
python3 test_local.py
```

For Android devices, storage permissions may be required before Python can access files under shared storage.

For example, Termux commonly requires:

```bash
termux-setup-storage
```

Then files may be accessible under:

```text
/storage/emulated/0/
```

Example:

```python
from Sharelink import ShareManager

with ShareManager() as manager:
    share = manager.create_share(
        "/storage/emulated/0/Download/example.zip"
    )

    print(share.public_url)

    input("Press Enter to stop...")
```

> Android compatibility depends on the device architecture and whether the required `cloudflared` binary can run in the environment.

---

# 🪵 Logging

PortaLink provides application logging with rotating log files.

The default log directory is:

```text
~/.sharelink/logs/
```

The default log filename is:

```text
sharelink.log
```

Rotated logs are retained for up to seven backups by default.

Logging can be configured through `ShareConfig` and `configure_logging()`.

---

# 🏗️ Project Architecture

The project is divided into several modules:

```text
PortaLink/
│
├── __init__.py       Public package exports
├── api.py            Public API and ShareManager interface
├── config.py         Configuration and defaults
├── dashboard.py      Dashboard rendering
├── download.py       Local / HTTP / FTP streaming
├── logger.py         Logging system
├── manager.py        Share lifecycle management
├── models.py         Data models and states
├── server.py         Embedded HTTP server
├── session.py        Individual share sessions
├── tunnel.py         Cloudflare tunnel lifecycle
└── utils.py          Shared utilities
```

---

# 🔄 Share Lifecycle

A share generally follows this lifecycle:

```text
                create_share()
                      │
                      ▼
                  RESOLVING
                      │
                      ▼
               SERVER STARTED
                      │
                      ▼
              TUNNEL CONNECTING
                      │
                      ▼
                   ACTIVE
                      │
          ┌───────────┼───────────┐
          │           │           │
          ▼           ▼           ▼
       Expired    Download     Deleted
          │       limit hit       │
          │           │           │
          └──────┬────┴───────────┘
                 ▼
              TERMINAL
                 │
                 ▼
               CLEANUP
```

---

# 🔐 Security Considerations

PortaLink is specifically designed to make resources accessible from outside the local network.

That is powerful, but it means you should treat every generated public URL as a real internet-facing access point.

### Do not share sensitive files accidentally.

For example, avoid exposing:

```text
~/.ssh/
private keys
password databases
browser profiles
API keys
.env files
personal documents
system directories
```

unless you explicitly intend to share them.

### Use short expiration times

For temporary transfers:

```python
expire_seconds=900
```

means the share expires after 15 minutes.

### Use download limits

For one-time sharing:

```python
max_downloads=1
```

### Revoke shares when finished

```python
manager.delete_share(share.share_id)
```

---

# ⚠️ Important: Public URL ≠ Private URL

A PortaLink URL is intended to be reachable from the public internet.

Do not assume that because the original resource is private:

```text
http://192.168.x.x/...
```

the resulting PortaLink URL is private.

The purpose of PortaLink is precisely to bridge that private resource to a public endpoint.

---

# 🔒 Privacy

PortaLink itself does not function as a permanent cloud-storage service.

The source remains on the source system or remote source and is served through the Sharelink process.

However, the public tunnel is provided by **Cloudflare**, and traffic passes through the tunnel infrastructure required to make the resource publicly reachable.

Review the applicable Cloudflare terms and privacy documentation before using PortaLink for sensitive or regulated data.

---

# ☁️ Cloudflare Dependency

Sharelink uses **Cloudflare `cloudflared`** to establish the public tunnel.

PortaLink can automatically download the binary for supported Linux architectures when no suitable installation is found.

Supported automatic Linux architectures currently include:

```text
x86_64
aarch64
armv7l
```

If `cloudflared` is already installed, PortaLink attempts to use the existing executable before downloading another copy.

You can also configure a custom binary path:

```python
from pathlib import Path
from PortaLink import ShareConfig

config = ShareConfig(
    cloudflared_binary_path=Path("/path/to/cloudflared")
)
```

---

# 🧪 Examples Included

The repository includes simple test/example scripts.

### Local file

```bash
python3 test_local.py
```

### HTTP/HTTPS source

```bash
python3 test_http.py
```

Before running the HTTP example, replace the example URL in the script with a URL that is accessible from your machine.

---

# 🛠️ Troubleshooting

## `cloudflared` cannot start

Check that:

- Your device architecture is supported.
- Your system allows execution of the downloaded binary.
- You have an internet connection.
- The Cloudflare release can be reached.
- The cached binary is executable.

You can also install `cloudflared` manually and make sure it is available in your `PATH`.

---

## Public URL does not appear

PortaLink waits for `cloudflared` to announce the tunnel URL.

You can increase the timeout:

```python
share = manager.create_share(
    "/path/to/file.zip",
    wait_for_url=True,
    url_timeout=120,
)
```

If the timeout expires, the tunnel can continue connecting in the background. You can wait again using:

```python
url = share.wait_for_url(timeout=120)

print(url)
```

---

## Local file cannot be found

Use an absolute path when possible:

```python
share = manager.create_share(
    "/home/user/Downloads/example.zip"
)
```

On Android/Termux:

```python
share = manager.create_share(
    "/storage/emulated/0/Download/example.zip"
)
```

---

## Port already in use

The default local server port is:

```text
8080
```

You can choose another port:

```python
from PortaLink import ShareConfig

config = ShareConfig(
    port=9090
)
```

---

# 🧑‍💻 Using PortaLink in Your Own Project

PortaLink is designed as a Python library, so it can be integrated into other applications.

For example:

```python
from PortaLink import ShareManager

def create_public_file_link(file_path):
    with ShareManager() as manager:
        share = manager.create_share(
            file_path,
            expire_seconds=3600,
            max_downloads=10,
        )

        return share.public_url
```

This makes it possible to integrate PortaLink with:

- Telegram bots
- Discord bots
- Web applications
- File managers
- Automation scripts
- Personal cloud systems
- Download tools
- Backup systems
- Desktop applications
- Android/Termux utilities

---

# 🤖 Automation Example

A program can create a link and automatically use it somewhere else:

```python
from PortaLink import ShareManager

with ShareManager() as manager:
    share = manager.create_share(
        "/path/to/video.mp4",
        expire_seconds=1800,
        max_downloads=5,
    )

    public_url = share.public_url

    print(public_url)

    # Use public_url with your own application/API here.
```

---

# 📦 Project Goals

PortaLink is designed around a simple idea:

> **If a resource is reachable from your machine, make it temporarily reachable from anywhere without first uploading it to a separate file-hosting service.**

The project aims to remain:

- Lightweight
- Python-based
- Easy to embed
- Easy to understand
- Useful on computers and Linux-based environments
- Suitable for automation
- Focused on temporary resource sharing

---

# 🗺️ Roadmap

Possible future improvements include:

- [ ] More tunnel providers
- [ ] Optional custom domains
- [ ] Authentication/password-protected shares
- [ ] Better web dashboard
- [ ] QR code generation
- [ ] More advanced access controls
- [ ] Improved Android/Termux integration
- [ ] More source protocols
- [ ] Additional download statistics
- [ ] Better CLI interface
- [ ] Plugin architecture
- [ ] More automated tests
- [ ] Packaging for PyPI

The roadmap may change as the project develops.

---

# 🤝 Contributing

Contributions, bug reports, feature requests, and improvements are welcome.

Before submitting a pull request:

1. Keep changes focused.
2. Follow the existing project structure.
3. Avoid unnecessary dependencies.
4. Document public APIs.
5. Test changes against local files and remote resources where applicable.
6. Do not include private URLs, credentials, API keys, tokens, or personal data.

For bugs, please include:

```text
Operating system:
Python version:
Architecture:
Sharelink version/commit:
Source type:
Error message:
Steps to reproduce:
```

---

# 🐛 Reporting Security Issues

If you discover a security vulnerability, please do not immediately publish full exploit details in a public issue.

Instead, contact the maintainer privately:

**Email:** `torekulislamtushar@gmail.com`

Replace the address above with the project's official security contact before publishing the repository.

---

# 📜 License

PortaLink is distributed under the license included in this repository.

See:

```text
LICENSE
```

for the complete terms.

## Attribution

PortaLink was created and is maintained by:

**Mr.Snow**

Copyright © 2026 **Mr.Snow**

When redistributing or modifying the project, retain the original copyright and attribution notices according to the applicable license.

---

# 💼 Commercial / Enterprise Use

Sharelink is intended to remain freely available for personal, educational, research, and other permitted uses under the project's license.

For **commercial, enterprise, SaaS, hosted-service, managed-service, or other business use**, please contact the author before deployment if required by the project's license.

### Commercial contact

**Author:** Mr.Snow  
**Email:** torekulislamtushar@gmail.com  
**GitHub:** `https://github.com/mrSnow-tr`

The author may provide written permission or a separate commercial agreement where applicable.

> **Important:** The exact commercial-use restrictions must be defined in `LICENSE`. This README is informational and does not replace the legal license.

---

# 👤 Author

Created by **Mr.Snow**.

PortaLink started as a personal project for solving a simple problem:

**How can a file or resource that is accessible from my local environment be shared through a temporary public link without first uploading it to cloud storage?**

The project is developed with the goal of keeping the tool lightweight, practical, and useful for automation.

---

# ⭐ Support the Project

If PortaLink is useful to you:

- ⭐ Star the repository
- 🐛 Report bugs
- 💡 Suggest features
- 🔧 Submit improvements
- 📖 Improve the documentation
- 📢 Share the project with others

---

# 📌 Disclaimer

PortaLink is a networking and file-sharing tool.

The author does not control what users choose to expose through it.

Users are responsible for:

- The files/resources they share
- Their network configuration
- The legality of the content they distribute
- Their credentials
- Their use of third-party services
- Compliance with applicable laws and service terms

Do not use PortaLink to expose data that you do not have permission to share.

---

## Credits

### PortaLink

Created by **Mr.Snow**

### Cloudflare Tunnel

Public tunneling is provided through **Cloudflare `cloudflared`**.

PortaLink is an independent project and is not affiliated with or endorsed by Cloudflare.

---

<div align="center">

**PortaLink**

### Private resources → Temporary public links

Made by **Mr.Snow**

</div>
