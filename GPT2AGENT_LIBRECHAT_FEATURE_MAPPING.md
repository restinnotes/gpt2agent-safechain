# gpt2agent -> LibreChat Feature Gap & Implementation Mapping

## 1. Executive Summary
This document provides the complete architecture and mapping specifications for advanced ChatGPT Web capabilities (`gpt2agent`) running inside LibreChat as the local conversation platform.

---

## 2. Core Operational Modes & Persistence Guarantee

| Feature | ChatGPT Web Upstream | LibreChat Local Instance |
|---|---|---|
| **History & Training** | `history_and_training_disabled = true` (Temporary Chat enforced) | `conversation history = always saved in MongoDB` |
| **Conversation Tree & Branching** | Stateless / ephemeral on OpenAI servers | Full DAG branching (`messageId`, `parentMessageId`, `children`) |
| **Message Navigation** | Not supported upstream in Temporary mode | Native `MessageNav` & sibling switching preserved |
| **Authentication & Users** | ChatGPT Team Bearer Token via `~/.codex/auth.json` | Multi-user JWT auth in LibreChat |

---

## 3. Advanced Capabilities Mapping Matrix

### 3.1 File Attachments (Local vs Upstream)
- **Status**: Implemented & Ready.
- **Mechanism**:
  1. User uploads a file (PDF, TXT, DOCX, Code) via LibreChat UI.
  2. LibreChat stores file locally in `api/uploads/` and records metadata in MongoDB `File` collection.
  3. When a conversation turn executes:
     - `gpt2agent-adapter` invokes `gpt2agent.sse.ConversationClient.upload_attachment(path, temporary=True)`.
     - File is uploaded to Azure BlockBlob temporary container via `/backend-api/files`.
     - Attachment reference is injected into the temporary chat turn payload.
  4. ChatGPT analyzes the file within the temporary session without permanent cloud storage.

### 3.2 Image Generation (DALL-E / ChatGPT Images)
- **Status**: Native Markdown / Content Part Mapping.
- **gpt2agent Raw Event**: Returns generated file URL / `file_id` or markdown image tag `![image](url)`.
- **LibreChat Normalization**:
  - Adapter converts markdown image URLs or emits OpenAI-compatible multimodal content parts.
  - Rendered inline in LibreChat chat window with lightbox zooming and download support.

### 3.3 Code Interpreter (Advanced Data Analysis)
- **Status**: Structured Event Normalization.
- **gpt2agent Raw Event**: Emits tool execution blocks:
  - Input: `python` code blocks
  - Output: stdout, stderr, image outputs (e.g. Matplotlib charts), and downloadable output files (`/mnt/data/...`).
- **LibreChat Normalization**:
  - Maps tool code blocks into collapsible execution blocks (Code / Result tabs).
  - Generated files/charts are saved locally and rendered as interactive artifact cards.

### 3.4 Canvas / Artifacts
- **Status**: LibreChat Artifact Viewer Integration.
- **Mechanism**:
  - gpt2agent Canvas edits generate structured code/text documents with revision metadata.
  - LibreChat's native Artifact Renderer (`packages/api/src/artifacts`) detects document tags or code fences and opens the right-side Artifact Viewer pane with live preview, syntax highlighting, and version comparison.

---

## 4. Architecture Diagram

```
+-------------------------------------------------------------+
|                      LibreChat Web UI                       |
|        (Browser, Message Tree, Branching, MessageNav)       |
+------------------------------+------------------------------+
                               |
                               | HTTP / SSE
                               v
+-------------------------------------------------------------+
|                    LibreChat Node Backend                   |
|                   (Express / Port 3080)                     |
|           - MongoDB Persistence (Local Source of Truth)     |
|           - User Auth & Multi-User Management               |
|           - Custom Endpoint: "gpt2agent"                    |
+------------------------------+------------------------------+
                               |
                               | OpenAI-compatible HTTP / SSE
                               v
+-------------------------------------------------------------+
|                  gpt2agent-adapter Gateway                  |
|                   (FastAPI / Port 9090)                     |
|           - Dynamic Model Discovery (/v1/models)            |
|           - Multi-Turn History Reconstruction               |
|           - thinking_effort Pass-Through                    |
|           - temporary=True Enforcement                      |
+------------------------------+------------------------------+
                               |
                               | Python Direct API
                               v
+-------------------------------------------------------------+
|                       gpt2agent Core                        |
|   (BackendClient, ConversationClient, Sentinel 2-Call Flow) |
+------------------------------+------------------------------+
                               |
                               | HTTPS / WSS
                               v
+-------------------------------------------------------------+
|                    ChatGPT Web (Upstream)                   |
|                   (Temporary Chat Session)                  |
+-------------------------------------------------------------+
```
