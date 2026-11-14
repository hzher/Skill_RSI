FROM toolathlon_pack-toolathlon:latest

ENV UV_INDEX_URL=https://pypi.org/simple
ENV UV_HTTP_TIMEOUT=60 UV_HTTP_RETRIES=1

# Compatibility fix from the previous task image; no local model server.
RUN uv pip install --python /opt/venv/bin/python 'mcp==1.9.0' \
    && /opt/venv/bin/python -c 'from mcp.server import FastMCP; from camel.agents import ChatAgent'

RUN uv pip install --python /opt/venv/bin/python \
    'PyPDF2==3.0.1' 'PyMuPDF==1.26.0' 'pdfplumber==0.11.7' 'pypdf==5.6.0' \
    && /opt/venv/bin/python -c 'import PyPDF2, fitz, pdfminer, pdfplumber, pypdf'

RUN set -eu; \
    for dir in 12306-mcp notion-mcp-server mcp-canvas-lms mcp-npx-fetch playwright-mcp woocommerce-mcp; do \
      cd "/opt/local_servers/$dir"; \
      npm ci --ignore-scripts; \
      case "$dir" in \
        notion-mcp-server|mcp-canvas-lms|woocommerce-mcp) \
          npm install --no-save --ignore-scripts pg@8.20.0 @types/pg@8.18.0 ;; \
      esac; \
      npm run build; \
    done

# The upstream base tolerates a missing Python Playwright CLI. Use the
# task's pinned Node Playwright package to install its matching browser.
RUN cd /opt/local_servers/playwright-mcp \
    && ./node_modules/.bin/playwright install chromium

RUN set -eu; uv python install 3.12.11; \
    for dir in yahoo-finance-mcp Office-Word-MCP-Server mcp-google-sheets \
      mcp-snowflake-server mcp-scholarly cli-mcp-server excel-mcp-server \
      Office-PowerPoint-MCP-Server emails-mcp pdf-tools-mcp arxiv-mcp-server arxiv-latex-mcp mcp-youtube-transcript; do \
      uv venv --clear --python 3.12.11 "/opt/local_servers/$dir/.venv"; \
      uv pip install --python "/opt/local_servers/$dir/.venv/bin/python" \
        -e "/opt/local_servers/$dir" mcp==1.30.0; \
    done

# Each configured uv run uses its installed project environment. Do not
# resolve development groups or stale upstream lock download URLs per task.
ENV UV_NO_SYNC=1 UV_PYTHON=3.12.11

COPY patch_toolathlon_email.py patch_toolathlon_notion.py patch_toolathlon_canvas.py patch_toolathlon_woocommerce.py /tmp/
RUN /opt/venv/bin/python /tmp/patch_toolathlon_email.py \
    && /opt/venv/bin/python /tmp/patch_toolathlon_notion.py \
    && npm --prefix /opt/local_servers/notion-mcp-server run build \
    && /opt/venv/bin/python /tmp/patch_toolathlon_canvas.py \
    && npm --prefix /opt/local_servers/mcp-canvas-lms run build \
    && /opt/venv/bin/python /tmp/patch_toolathlon_woocommerce.py \
    && npm --prefix /opt/local_servers/woocommerce-mcp run build

RUN test -x /opt/local_servers/emails-mcp/.venv/bin/emails-mcp

COPY patch_toolathlon_runtime.py /tmp/patch_toolathlon_runtime.py
RUN /opt/venv/bin/python /tmp/patch_toolathlon_runtime.py --root /opt/local_servers \
    && npm --prefix /opt/local_servers/google-forms-mcp run build

RUN uv pip install --python /opt/venv/bin/python \
    numpy==2.2.6 pandas==2.2.3 scipy==1.15.3 \
    && /opt/venv/bin/python -c 'import openpyxl, docx, scipy, pandas; print("Task Python libraries OK")'

# Exercise the service entrypoint, not only installed-package metadata.
RUN uv pip install --python /opt/local_servers/arxiv-mcp-server/.venv/bin/python \
      'pymupdf4llm==0.0.17' 'PyMuPDF==1.26.0' \
    && /opt/local_servers/arxiv-mcp-server/.venv/bin/python -c \
      'import pymupdf4llm, fitz, arxiv_mcp_server; print("arXiv MCP entrypoint import OK")'

RUN uv pip install --python /opt/venv/bin/python 'beautifulsoup4==4.13.4' \
    && /opt/venv/bin/python -c 'import bs4'
COPY patch_toolathlon_runtime2.py /tmp/patch_toolathlon_runtime2.py
RUN /opt/venv/bin/python /tmp/patch_toolathlon_runtime2.py \
    && npm --prefix /opt/local_servers/mcp-canvas-lms run build \
    && npm --prefix /opt/local_servers/woocommerce-mcp run build
