/* =============================================
   DOM ELEMENT REFERENCES
   ============================================= */
const chatContainer   = document.getElementById('chat-container');
const chatDisplay     = document.getElementById('chat-display');
const userInput       = document.getElementById('user-input');
const sendBtn         = document.getElementById('send-btn');
const typingIndicator = document.getElementById('typing-indicator');
const loginOverlay    = document.getElementById('login-overlay');
const appRoot         = document.getElementById('app');
const pwInput         = document.getElementById('pw-input');
const loginBtn        = document.getElementById('login-btn');
const loginError      = document.getElementById('login-error');
const charSelect      = document.getElementById('char-select');
const modelSelect     = document.getElementById('model-select');
const newChatBtn      = document.getElementById('new-chat-btn');
const delChatBtn      = document.getElementById('del-chat-btn');
const chatList        = document.getElementById('chats-list');
const chatsSearchInput   = document.getElementById('chats-search');
const chatsEditToggle    = document.getElementById('chats-edit-toggle');
const chatsEditToggleLbl = document.getElementById('chats-edit-toggle-label');
const chatsEditbar       = document.getElementById('chats-editbar');
const chatsDeleteBtn     = document.getElementById('chats-delete-btn');
const chatsEditbarDone   = document.getElementById('chats-editbar-done');
const settingsBtn     = document.getElementById('settings-btn');
const confirmModal    = document.getElementById('confirm-modal');
const modalConfirm    = document.getElementById('modal-confirm');
const modalCancel     = document.getElementById('modal-cancel');
const settingsModal   = document.getElementById('settings-modal');
const settingsClose   = document.getElementById('settings-close');
const thoughtsToggle  = document.getElementById('thoughts-toggle');
const thoughtsLabel   = document.getElementById('thoughts-label');
const logPanel        = document.getElementById('log-panel');
const logOutput       = document.getElementById('log-output');
const logStatus       = document.getElementById('log-status');
const logBadge        = document.getElementById('log-badge');
const logToggleBtn    = document.getElementById('log-toggle-btn');
const logClearBtn     = document.getElementById('log-clear-btn');
const logCloseBtn     = document.getElementById('log-close-btn');
const charManageBtn   = document.getElementById('char-manage-btn');
const sidebarWrapEl        = document.getElementById('sidebar-wrap');
const sidebarCollapseBtn   = document.getElementById('sidebar-collapse-btn');
const charpanelWrapEl      = document.getElementById('charpanel-wrap');
const charpanelCollapseBtn = document.getElementById('charpanel-collapse-btn');
const charpanelImage       = document.getElementById('charpanel-image');
const activeBarAvatar      = document.getElementById('active-bar-avatar');
const charpanelEmpty       = document.getElementById('charpanel-empty');
const navChatsBtn          = document.getElementById('nav-chats');
const chatsDrawer          = document.getElementById('chats-drawer');
const chatsDrawerBackdrop  = document.getElementById('chats-drawer-backdrop');
const chatsDrawerCloseBtn  = document.getElementById('chats-drawer-close');
const scrollBottomBtn = document.getElementById('scroll-bottom-btn');
const charChatToggle   = document.getElementById('char-chat-toggle');
const charChatDropdown = document.getElementById('char-chat-dropdown');

function openChatsDrawer() {
  chatsDrawer.classList.remove('hidden');
  chatsDrawerBackdrop.classList.remove('hidden');
  navChatsBtn.classList.add('active');
}
function closeChatsDrawer() {
  chatsDrawer.classList.add('hidden');
  chatsDrawerBackdrop.classList.add('hidden');
  navChatsBtn.classList.remove('active');
}
const charpanelPrev        = document.getElementById('charpanel-prev');
const charpanelNext        = document.getElementById('charpanel-next');
const charpanelCounter     = document.getElementById('charpanel-counter');
const charpanelThumbs      = document.getElementById('charpanel-thumbs');
const charpanelName        = document.getElementById('charpanel-name');
const charpanelMeta        = document.getElementById('charpanel-meta');
const charpanelDesc        = document.getElementById('charpanel-desc');
const charpanelSeeMore     = document.getElementById('charpanel-seemore');
const charpanelDescRow     = document.querySelector('.charpanel-desc-row');
const charpanelDescEditBtn = document.getElementById('charpanel-desc-edit');
const charpanelDescEditBox = document.getElementById('charpanel-desc-editbox');
const charpanelDescTextarea= document.getElementById('charpanel-desc-textarea');
const charpanelDescSave    = document.getElementById('charpanel-desc-save');
const charpanelDescCancel  = document.getElementById('charpanel-desc-cancel');
const charpanelAddImageInput = document.getElementById('charpanel-add-image-input');

// AI Character Writer modal
const aiCharWriterBtn      = document.getElementById('ai-char-writer-btn');
const charWriterModal      = document.getElementById('char-writer-modal');
const charWriterInput      = document.getElementById('char-writer-input');
const charWriterStatus     = document.getElementById('char-writer-status');
const charWriterClose      = document.getElementById('char-writer-close');
const charWriterCancelBtn  = document.getElementById('char-writer-cancel');
const charWriterGenerateBtn= document.getElementById('char-writer-generate');
const charWriterSpinner    = document.getElementById('char-writer-spinner');
const charWriterCounter    = document.getElementById('char-writer-counter');

/* =============================================
   APP STATE
   ============================================= */

let isSending                 = false;
let abortController           = null;
let backend_current_path      = null;
let backend_current_chat_id   = null;
let backend_current_character = null;
let logUnreadCount            = 0;
let logPanelOpen              = false;
let logPollTimer              = null;
let logLastId                 = 0;
let charWriterStatusInterval  = null;

/* =============================================
   SIDEBAR COLLAPSE
   ============================================= */
/* =============================================
   SIDE PANEL COLLAPSE (shared logic for left sidebar + right character panel)
   ============================================= */
function setPanelCollapsed(wrapEl, btnEl, storageKey, collapsed) {
  if (!wrapEl || !btnEl) return;
  wrapEl.classList.toggle('panel--collapsed', collapsed);
  btnEl.setAttribute('aria-expanded', String(!collapsed));
  const label = collapsed ? 'Expand' : 'Minimize';
  btnEl.title = label;
  const tooltip = btnEl.querySelector('.panel-collapse-tooltip');
  if (tooltip) tooltip.textContent = label;
  sessionStorage.setItem(storageKey, collapsed ? '1' : '0');
}

function toggleSidebar() {
  if (!sidebarWrapEl) return;
  setPanelCollapsed(
    sidebarWrapEl, sidebarCollapseBtn, 'mc_sidebar_collapsed',
    !sidebarWrapEl.classList.contains('panel--collapsed')
  );
}

function toggleCharPanel() {
  if (!charpanelWrapEl) return;
  setPanelCollapsed(
    charpanelWrapEl, charpanelCollapseBtn, 'mc_charpanel_collapsed',
    !charpanelWrapEl.classList.contains('panel--collapsed')
  );
}

/* =============================================
   INIT
   ============================================= */
function init() {
  populateSelects();
  attachEventListeners();
  // Log panel no longer auto-connects on load — it only polls while
  // actually open, so a normal page visit never ties up the server.
  makeDraggable(logPanel, document.getElementById('log-panel-header'));
  logToggleBtn.addEventListener('click', toggleLogPanel);
  logCloseBtn.addEventListener('click',  closeLogPanel);
  logClearBtn.addEventListener('click',  clearLog);

  if (sidebarCollapseBtn) sidebarCollapseBtn.addEventListener('click', toggleSidebar);
  if (sidebarWrapEl && sessionStorage.getItem('mc_sidebar_collapsed') === '1') {
    setPanelCollapsed(sidebarWrapEl, sidebarCollapseBtn, 'mc_sidebar_collapsed', true);
  }

  if (charpanelCollapseBtn) charpanelCollapseBtn.addEventListener('click', toggleCharPanel);
  if (charpanelWrapEl && sessionStorage.getItem('mc_charpanel_collapsed') === '1') {
    setPanelCollapsed(charpanelWrapEl, charpanelCollapseBtn, 'mc_charpanel_collapsed', true);
  }
  initCharPanel();

  if (sessionStorage.getItem('mc_authenticated') === '1') {
    loginOverlay.classList.add('hidden');
    appRoot.classList.remove('hidden');
    updateActiveBar();
    setTimeout(triggerWarmUp, 1000);
  } else {
    pwInput.focus();
  }
}

/* =============================================
   POPULATE DROPDOWNS
   ============================================= */
function populateSelects() {
  fetch('/characters')
    .then(r => r.json())
    .then(chars => {
      chars.forEach(char => {
        const opt = document.createElement('option');
        opt.value       = char;
        opt.textContent = char;
        charSelect.appendChild(opt);
      });
      if (chars.length > 0) {
        refreshChatList();
        updateActiveBar();
      }

      // --- Arrived from the home page character grid (?character=Name) ---
      // or from the home page's Chats list (?character=Name&chat=<path>),
      // which should resume that exact saved chat instead of starting new.
      const params    = new URLSearchParams(window.location.search);
      const preselect = params.get('character');
      const chatPath  = params.get('chat');
      if (preselect && chars.includes(preselect)) {
        charSelect.value = preselect;
        updateActiveBar(); // syncs the right sidebar (image, description, meta)
                            // to the preselected character — handleNewChat()
                            // below does not call this itself.
        if (chatPath) {
          loadChat(decodeURIComponent(chatPath));
        } else {
          // No specific chat requested — resume this character's most
          // recent chat from any previous session, rather than always
          // starting a fresh one. /chat_history_all is already sorted
          // most-recent-first across all characters.
          fetch('/chat_history_all')
            .then(r => r.json())
            .then(chats => {
              const match = Array.isArray(chats)
                ? chats.find(c => c.character === preselect)
                : null;
              if (match) {
                loadChat(match.path);
              } else {
                handleNewChat(); // genuinely no prior chat for this character
              }
            })
            .catch(() => handleNewChat());
        }
      }
    })
    .catch(() => {
      console.warn('Could not reach /characters — is server.py running?');
    });

  fetch('/models')
    .then(r => r.json())
    .then(models => {
      models.forEach(model => {
        const opt = document.createElement('option');
        opt.value       = model;
        opt.textContent = model;
        modelSelect.appendChild(opt);
      });
      updateActiveBar();
    })
    .catch(() => {
      console.warn('Could not reach /models — is server.py running?');
    });
}

/* =============================================
   CHAT LIST — universal across every character,
   most-recently-active chat first (like a normal
   messaging inbox, not one list per character). Mirrors home.html's
   Chats view (search, edit-mode multi-select, bulk delete) so both
   pages present the identical UI — the only difference is that a row
   click here loads the chat in-page instead of navigating away, since
   we're already on the chat page.
   ============================================= */
const CHAT_ITEM_GRADIENTS = [
  "linear-gradient(135deg, #3a3a3f, #6d6d75)",
  "linear-gradient(135deg, #52525a, #8a8a92)",
  "linear-gradient(135deg, #2b2b30, #58585f)",
  "linear-gradient(135deg, #45454c, #78787f)",
];

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function chatItemInitials(name) {
  return String(name).split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();
}

function formatChatDate(epochSeconds) {
  if (!epochSeconds) return "";
  const date = new Date(epochSeconds * 1000);
  return date.toLocaleDateString("en-US", { month: "numeric", day: "numeric", year: "numeric" });
}

let ALL_CHATS      = [];
let chatsEditMode   = false;
const selectedChats = new Map(); // path -> {path, character, chat_id}

function refreshChatList() {
  chatList.innerHTML = `<div class="chats-empty-state">Loading chats…</div>`;
  fetch("/chat_history_all")
    .then(r => r.json())
    .then(chats => {
      ALL_CHATS = Array.isArray(chats) ? chats : [];
      renderChatsList(ALL_CHATS.filter(matchesChatsSearch));
    })
    .catch(() => {
      chatList.innerHTML = `<div class="chats-empty-state">Could not reach the server — is server.py running?</div>`;
    });
}

function renderChatsList(list) {
  chatList.innerHTML = "";

  if (list.length === 0) {
    chatList.innerHTML = `<div class="chats-empty-state">No chats yet — start one from a character's page.</div>`;
    return;
  }

  list.forEach((chat, i) => {
    const row = document.createElement("div");
    row.className = "chat-row";
    if (chat.path === backend_current_path) row.classList.add("active");

    const preview = (chat.preview || "").replace(/\s+/g, " ").trim() || "No messages yet";
    const isPlaceholder = !!chat.preview_is_placeholder;

    row.innerHTML = `
      <div class="chat-row-select" data-path="${escapeHtml(chat.path)}"></div>
      <div class="chat-row-avatar" style="background:${CHAT_ITEM_GRADIENTS[i % CHAT_ITEM_GRADIENTS.length]}">
        <span class="chat-row-avatar-initials">${chatItemInitials(chat.character || "?")}</span>
        <img alt="" style="display:none">
      </div>
      <div class="chat-row-body">
        <div class="chat-row-top">
          <span class="chat-row-name">${escapeHtml(chat.character || "Unknown")}</span>
          <span class="chat-row-date">${formatChatDate(chat.last_activity)}</span>
        </div>
        <div class="chat-row-preview${isPlaceholder ? " placeholder" : ""}">${escapeHtml(preview)}</div>
      </div>
    `;

    // Try a real avatar; fall back to the gradient + initials placeholder.
    const img = row.querySelector(".chat-row-avatar img");
    const initialsSpan = row.querySelector(".chat-row-avatar-initials");
    img.onload  = () => { img.style.display = "block"; initialsSpan.style.display = "none"; };
    img.onerror = () => { img.style.display = "none"; initialsSpan.style.display = "block"; };
    if (chat.char_id) img.src = `/character_image/${encodeURIComponent(chat.char_id)}`;

    const selectCircle = row.querySelector(".chat-row-select");
    if (selectedChats.has(chat.path)) selectCircle.classList.add("checked");

    row.addEventListener("click", () => {
      if (chatsEditMode) {
        toggleChatSelection(chat, selectCircle);
      } else {
        loadChat(chat.path);
        closeChatsDrawer();
      }
    });

    chatList.appendChild(row);
  });
}

function matchesChatsSearch(chat) {
  const q = (chatsSearchInput.value || "").trim().toLowerCase();
  if (!q) return true;
  return (chat.character || "").toLowerCase().includes(q) || (chat.preview || "").toLowerCase().includes(q);
}

function toggleChatSelection(chat, circleEl) {
  if (selectedChats.has(chat.path)) {
    selectedChats.delete(chat.path);
    circleEl.classList.remove("checked");
  } else {
    selectedChats.set(chat.path, chat);
    circleEl.classList.add("checked");
  }
  chatsDeleteBtn.disabled = selectedChats.size === 0;
}

function enterChatsEditMode() {
  chatsEditMode = true;
  selectedChats.clear();
  chatList.classList.add("edit-mode");
  chatsEditToggle.classList.add("active");
  chatsEditToggleLbl.textContent = "Done";
  chatsEditbar.classList.remove("hidden");
  chatsDeleteBtn.disabled = true;
  renderChatsList(ALL_CHATS.filter(matchesChatsSearch));
}

function exitChatsEditMode() {
  chatsEditMode = false;
  selectedChats.clear();
  chatList.classList.remove("edit-mode");
  chatsEditToggle.classList.remove("active");
  chatsEditToggleLbl.textContent = "Edit";
  chatsEditbar.classList.add("hidden");
}

function loadChat(path) {
  backend_current_path = path;
  fetch('/load_chat', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ path })
  })
  .then(r => r.json())
  .then(data => {
    if (data.status !== 'ok') return;
    backend_current_chat_id = data.chat_id; // <-- ADD THIS LINE

    // A chat opened from the universal history list can belong to a
    // different character than the one currently selected — keep the
    // dropdown/header in sync. Setting .value here does NOT fire the
    // 'change' listener (that only fires on user interaction), so this
    // won't trigger handleNewChat() and wipe the chat we just loaded.
    if (data.character && charSelect.value !== data.character) {
      charSelect.value = data.character;
      updateActiveBar();
    }

    chatContainer.innerHTML = '';
// ...
    data.messages.forEach(msg => {
      // Pass 'true' to skip the heavy scrolling calculations per message
      if (msg.role === 'user')           addUserMessage(msg.content, true);
      else if (msg.role === 'assistant') addBotMessage(msg.content, true);
    });

    // Scroll exactly once after the entire chat history is painted to the screen
    setTimeout(scrollToBottom, 50);

    // Re-render the sidebar so the newly active chat is highlighted.
    refreshChatList();
  })
  .catch(() => {});
}

/* =============================================
   LOGIN
   ============================================= */
function checkPassword() {
  const entered = pwInput.value.trim();

  if (!entered) {
    loginError.textContent = 'Please enter a password.';
    pwInput.focus();
    return;
  }

  if (entered === 'admin') {
    loginError.textContent = '';
    sessionStorage.setItem('mc_authenticated', '1');
    window.location.href = 'home.html';
  } else {
    loginError.textContent = 'Please check your password and try again.';
    pwInput.value = '';
    pwInput.focus();
  }
}
/* =============================================
   HELPER: triggerWarmUp()
   Pings the backend to wake up the serverless GPU
   ============================================= */
function triggerWarmUp() {
  const model = modelSelect.value;
  if (!model) return;

  fetch('/warmup', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ model: model })
  }).catch(() => console.warn('Warm-up ping failed.'));
}

/* =============================================
   CORE: sendMessage() — with live streaming
   ============================================= */
function sendMessage() {
  const text = userInput.value.trim();

  if (!text)     return;
  if (isSending) return;

  isSending            = true;
  sendBtn.disabled     = true;
  userInput.disabled   = true;
  charSelect.disabled  = true;
  modelSelect.disabled = true;

  addUserMessage(text);
  userInput.value    = '';
  userInput.disabled = false;
  userInput.focus();

  showTyping(true);

  // Create the bot bubble immediately with a blinking cursor
  const botRow = document.createElement('div');
  botRow.classList.add('message-row', 'bot-row');

  const botAvatar = document.createElement('div');
  botAvatar.classList.add('avatar');
  botAvatar.textContent = '✨';

  const botMsgContent = document.createElement('div');
  botMsgContent.classList.add('msg-content');

  // --- MISSING VARIABLES RESTORED HERE ---
  const botMsgText = document.createElement('div');
  botMsgText.classList.add('msg-text', 'streaming');

  const cursor = document.createElement('span');
  cursor.classList.add('stream-cursor');
  cursor.textContent = '▍';
  botMsgText.appendChild(cursor);
  // ---------------------------------------

  botMsgContent.appendChild(botMsgText);
  botRow.appendChild(botAvatar);
  botRow.appendChild(botMsgContent);
  attachMessageActions(botRow, botMsgText, botMsgContent);
  chatContainer.appendChild(botRow);
  scrollToBottom();

  streamBotReply(text, botMsgText, cursor)
    .finally(() => {
      showTyping(false);
      isSending            = false;
      sendBtn.disabled     = false;
      charSelect.disabled  = false;
      modelSelect.disabled = false;
      userInput.focus();
    });
}

/* =============================================
   CORE: streamBotReply(text, targetEl, cursor)
/* =============================================
   CORE: addUserMessage(text)
   ============================================= */
function addUserMessage(text, skipScroll = false) {
  const row = document.createElement('div');
  row.classList.add('message-row', 'user-row');

  const avatar = document.createElement('div');
  avatar.classList.add('avatar');
  avatar.textContent = '👤';

  const msgContent = document.createElement('div');
  msgContent.classList.add('msg-content');

  const msgText = document.createElement('div');
  msgText.classList.add('msg-text');
  msgText.appendChild(parseActionText(text));
  msgContent.appendChild(msgText);

  row.appendChild(avatar);
  row.appendChild(msgContent);
  attachMessageActions(row, msgText, msgContent);
  chatContainer.appendChild(row);

  const spacer = document.createElement('div');
  spacer.classList.add('msg-spacer');
  chatContainer.appendChild(spacer);

  if (!skipScroll) scrollToBottom();
}

/* =============================================
   CORE: addBotMessage(text)
   ============================================= */
function addBotMessage(text, skipScroll = false) {
  const row = document.createElement('div');
  row.classList.add('message-row', 'bot-row');

  const avatar = document.createElement('div');
  avatar.classList.add('avatar');
  avatar.textContent = '✨';

  const msgContent = document.createElement('div');
  msgContent.classList.add('msg-content');

  const msgText = document.createElement('div');
  msgText.classList.add('msg-text');
  msgText.appendChild(parseActionText(text));
  msgContent.appendChild(msgText);

  row.appendChild(avatar);
  row.appendChild(msgContent);
  attachMessageActions(row, msgText, msgContent);
  chatContainer.appendChild(row);

  const separator = document.createElement('hr');
  separator.classList.add('msg-separator');
  chatContainer.appendChild(separator);

  if (!skipScroll) scrollToBottom();
}

/* =============================================
   CORE: addSceneImage(imageUrl, caption)
   ============================================= */
function addSceneImage(imageUrl, caption) {
  const wrapper = document.createElement('div');
  wrapper.classList.add('scene-image-wrapper');

  if (caption) {
    const cap = document.createElement('div');
    cap.classList.add('scene-image-caption');
    cap.textContent = caption;
    wrapper.appendChild(cap);
  }

  const img = document.createElement('img');
  img.classList.add('scene-image');
  img.src = imageUrl;
  img.alt = 'Generated scene';
  wrapper.appendChild(img);

  chatContainer.appendChild(wrapper);

  const separator = document.createElement('hr');
  separator.classList.add('msg-separator');
  chatContainer.appendChild(separator);

  scrollToBottom();
}

/* =============================================
   HELPER: parseActionText(text)
   ============================================= */
function parseActionText(text) {
  const fragment = document.createDocumentFragment();
  const parts    = text.split('*');

  parts.forEach((part, index) => {
    if (!part) return;
    if (index % 2 === 1) {
      const em = document.createElement('em');
      em.classList.add('action-text');
      em.textContent = part;
      fragment.appendChild(em);
    } else {
      fragment.appendChild(document.createTextNode(part));
    }
  });

  return fragment;
}

/* =============================================
   MESSAGE EDITING
   A hover-only copy + pencil pair sits at the BOTTOM
   of every message bubble (user or bot, past or
   present) — not beside the avatar — so it never
   overlaps the text. The index of a message is
   computed live from its position among .message-row
   elements at click/save time — not tracked at
   creation — so it stays correct across retry/undo/
   continue, which keep DOM rows and the saved
   messages[] array in the same order and count.
   ============================================= */
const COPY_ICON_SVG  = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const CHECK_ICON_SVG = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;

function createCopyButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.classList.add('msg-action-btn', 'msg-copy-btn');
  btn.title = 'Copy message';
  btn.innerHTML = COPY_ICON_SVG;
  return btn;
}

function createEditButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.classList.add('msg-action-btn', 'msg-edit-btn');
  btn.title = 'Edit message';
  btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;
  return btn;
}

// Inverse of parseActionText(): turns the currently-rendered DOM content of
// a .msg-text bubble back into plain text with *asterisks* around actions,
// so editing/copying shows exactly what was originally typed/generated.
function extractRawMessageText(msgTextEl) {
  let result = '';
  msgTextEl.childNodes.forEach(node => {
    if (node.nodeType === Node.TEXT_NODE) {
      result += node.textContent;
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.classList.contains('stream-cursor')) return; // skip blinking cursor artifacts
      if (node.classList.contains('action-text')) {
        result += `*${node.textContent}*`;
      } else {
        result += node.textContent;
      }
    }
  });
  return result;
}

// Legacy fallback for contexts where navigator.clipboard isn't available
// (e.g. serving the app over a plain http:// LAN address).
function legacyCopyToClipboard(text) {
  const temp = document.createElement('textarea');
  temp.value = text;
  temp.style.position = 'fixed';
  temp.style.opacity = '0';
  document.body.appendChild(temp);
  temp.focus();
  temp.select();
  try { document.execCommand('copy'); } catch (e) { /* no-op */ }
  document.body.removeChild(temp);
}
/* =============================================
   HELPER: handleChatScroll()
   Toggles the scroll-to-bottom button visibility
   ============================================= */
function handleChatScroll() {
  if (!chatDisplay || !scrollBottomBtn) return;

  // Show button if we are scrolled up more than 150px from the bottom
  const isNearBottom = chatDisplay.scrollHeight - chatDisplay.scrollTop - chatDisplay.clientHeight < 150;

  if (isNearBottom) {
    scrollBottomBtn.classList.add('hidden');
  } else {
    scrollBottomBtn.classList.remove('hidden');
  }
}

function copyMessageText(msgTextEl, btn) {
  const text = extractRawMessageText(msgTextEl);

  function flashCopied() {
    if (btn.dataset.copyResetTimer) clearTimeout(Number(btn.dataset.copyResetTimer));
    btn.innerHTML = CHECK_ICON_SVG;
    btn.classList.add('copied');
    const timer = setTimeout(() => {
      btn.innerHTML = COPY_ICON_SVG;
      btn.classList.remove('copied');
    }, 1200);
    btn.dataset.copyResetTimer = String(timer);
  }

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(flashCopied).catch(() => {
      legacyCopyToClipboard(text);
      flashCopied();
    });
  } else {
    legacyCopyToClipboard(text);
    flashCopied();
  }
}

function enableMessageEdit(row, msgTextEl) {
  if (row.classList.contains('editing')) return;
  if (msgTextEl.classList.contains('streaming')) return;

  const rawText = extractRawMessageText(msgTextEl);

  row.classList.add('editing');
  msgTextEl.style.display = 'none';

  const textarea = document.createElement('textarea');
  textarea.classList.add('msg-edit-textarea');
  textarea.value = rawText;

  const actions = document.createElement('div');
  actions.classList.add('msg-edit-actions');

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.classList.add('msg-edit-save-btn');
  saveBtn.textContent = 'Save';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.classList.add('msg-edit-cancel-btn');
  cancelBtn.textContent = 'Cancel';

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);

  const errorEl = document.createElement('div');
  errorEl.classList.add('msg-edit-error');

  msgTextEl.insertAdjacentElement('afterend', errorEl);
  msgTextEl.insertAdjacentElement('afterend', actions);
  msgTextEl.insertAdjacentElement('afterend', textarea);

  // Grow the textarea to fit the ENTIRE message right away (capped by
  // max-height in CSS, which adds its own scrollbar for anything huge),
  // instead of the old fixed ~3-line box that clipped long messages.
  function autoResizeTextarea() {
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight}px`;
  }
  autoResizeTextarea();
  textarea.addEventListener('input', autoResizeTextarea);

  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  function exitEditMode() {
    textarea.remove();
    actions.remove();
    errorEl.remove();
    msgTextEl.style.display = '';
    row.classList.remove('editing');
  }

  cancelBtn.addEventListener('click', exitEditMode);
  textarea.addEventListener('keydown', e => {
    if (e.key === 'Escape') exitEditMode();
  });

  saveBtn.addEventListener('click', () => {
    const newText = textarea.value;
    errorEl.textContent = '';

    if (!backend_current_path) {
      errorEl.textContent = 'No active chat file — try reloading the chat.';
      return;
    }

    const rows  = Array.from(chatContainer.querySelectorAll('.message-row'));
    const index = rows.indexOf(row);
    const role  = row.classList.contains('user-row') ? 'user' : 'assistant';

    if (index === -1) {
      errorEl.textContent = 'Could not locate this message — try reloading the chat.';
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    fetch('/edit_message', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ path: backend_current_path, index, role, content: newText })
    })
      .then(r => r.json().then(body => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        if (!ok || body.status !== 'success') {
          throw new Error(body.message || 'Failed to save the message.');
        }
        msgTextEl.innerHTML = '';
        msgTextEl.appendChild(parseActionText(newText));
        exitEditMode();
      })
      .catch(err => {
        errorEl.textContent = err.message;
      })
      .finally(() => {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
      });
  });
}

// Adds the hover copy + edit bar to the BOTTOM of a message — call right
// after building any .message-row + .msg-content + .msg-text trio,
// whether historical, freshly sent, or streamed. contentEl must be the
// block-level wrapper the text lives in (.msg-content), not the flex
// .message-row itself, or the bar ends up beside the text instead of
// underneath it.
function attachMessageActions(row, msgTextEl, contentEl) {
  const actionsBar = document.createElement('div');
  actionsBar.classList.add('msg-actions-bar');

  const copyBtn = createCopyButton();
  copyBtn.addEventListener('click', e => {
    e.stopPropagation();
    copyMessageText(msgTextEl, copyBtn);
  });

  const editBtn = createEditButton();
  editBtn.addEventListener('click', e => {
    e.stopPropagation();
    enableMessageEdit(row, msgTextEl);
  });

  actionsBar.appendChild(copyBtn);
  actionsBar.appendChild(editBtn);
  contentEl.appendChild(actionsBar);
}

/* =============================================
   HELPER: scrollToBottom()
   ============================================= */
let scrollQueued = false;

function scrollToBottom() {
  // SMART SCROLL: Only auto-scroll if the user is already near the bottom.
  // Uses requestAnimationFrame to prevent DOM Layout Thrashing during fast streams.
  if (!scrollQueued) {
    scrollQueued = true;
    requestAnimationFrame(() => {
      const isNearBottom = chatDisplay.scrollHeight - chatDisplay.scrollTop - chatDisplay.clientHeight < 150;
      if (isNearBottom) {
        chatDisplay.scrollTop = chatDisplay.scrollHeight;
      }
      scrollQueued = false;
    });
  }
}
/* =============================================
   HELPER: showTyping(visible)
   ============================================= */
function showTyping(visible) {
  if (visible) {
    const charName = charSelect.value || 'Assistant';
    typingIndicator.textContent = `${charName} is thinking...`;
  } else {
    typingIndicator.textContent = '';
  }
}

/* =============================================
   HELPER: addSystemMessage(text)
   ============================================= */
function addSystemMessage(text) {
  const div = document.createElement('div');
  div.classList.add('system-message');
  div.textContent = text;
  chatContainer.appendChild(div);
  scrollToBottom();
}

/* =============================================
   HELPER: updateActiveBar()
   Syncs the top bar to current character + model
   ============================================= */
function updateActiveBar() {
  const charName  = charSelect.value  || '—';

  const charEl  = document.getElementById('active-char-name');
  const dot     = document.querySelector('.active-bar-dot');

  if (charEl)  charEl.textContent  = charName;

  if (dot) {
    if (charName !== '—' && charName !== '') {
      dot.classList.add('live');
    } else {
      dot.classList.remove('live');
    }
  }

  loadCharacterPanel(charSelect.value);
}

/* =============================================
   CHARACTER PANEL (right sidebar)
   Pulls name/meta/description from /character/<name>, and the full image
   set (main avatar + any gallery extras) from /character_gallery/<id>.
   The "+" tile and per-thumbnail delete button POST straight to the new
   /add_character_image and /delete_character_image routes, and the
   pencil next to the description PATCHes a dedicated "description" field
   via the existing /edit_character endpoint.
   ============================================= */
let charpanelImages = [];  // [{url, filename}] — filename is null for the main avatar
let charpanelIndex  = 0;
let currentCharId   = null;
let currentCharName = null;

function initCharPanel() {
  if (charpanelPrev) charpanelPrev.addEventListener('click', () => shiftCharpanelImage(-1));
  if (charpanelNext) charpanelNext.addEventListener('click', () => shiftCharpanelImage(1));
  if (charpanelSeeMore) {
    charpanelSeeMore.addEventListener('click', () => {
      const expanded = charpanelDesc.classList.toggle('expanded');
      charpanelSeeMore.textContent = expanded ? 'See less' : 'See more';
    });
  }
  if (charpanelDescEditBtn) charpanelDescEditBtn.addEventListener('click', openDescEdit);
  if (charpanelDescCancel)  charpanelDescCancel.addEventListener('click', closeDescEdit);
  if (charpanelDescSave)    charpanelDescSave.addEventListener('click', saveDescEdit);
  if (charpanelAddImageInput) charpanelAddImageInput.addEventListener('change', handleAddImageFile);
  renderCharpanelEmpty();
}

function renderCharpanelEmpty() {
  charpanelImages = [];
  charpanelIndex  = 0;
  currentCharId   = null;
  currentCharName = null;
  if (charpanelImage) { charpanelImage.classList.add('hidden'); charpanelImage.src = ''; }
  if (activeBarAvatar) activeBarAvatar.src = '';
  if (charpanelEmpty) charpanelEmpty.classList.remove('hidden');
  if (charpanelCounter) charpanelCounter.classList.add('hidden');
  if (charpanelPrev) charpanelPrev.classList.add('hidden');
  if (charpanelNext) charpanelNext.classList.add('hidden');
  if (charpanelThumbs) { charpanelThumbs.innerHTML = ''; charpanelThumbs.classList.add('hidden'); }
  if (charpanelName) charpanelName.textContent = '—';
  if (charpanelMeta) charpanelMeta.textContent = '';
  if (charpanelDesc) { charpanelDesc.textContent = ''; charpanelDesc.classList.remove('expanded'); }
  if (charpanelSeeMore) { charpanelSeeMore.classList.add('hidden'); charpanelSeeMore.textContent = 'See more'; }
  if (charpanelDescEditBtn) charpanelDescEditBtn.classList.add('hidden');
  closeDescEdit();
}

async function loadCharacterPanel(name) {
  if (!charpanelImage) return; // panel not present on this page

  if (!name) {
    renderCharpanelEmpty();
    return;
  }

  try {
    const res = await fetch(`/character/${encodeURIComponent(name)}`);
    if (!res.ok) { renderCharpanelEmpty(); return; }
    const payload = await res.json();
    const charId  = payload.id;
    const data    = payload.data || {};
    const info    = data.data || data; // some cards nest fields under "data"

    currentCharId   = charId;
    currentCharName = data.name || info.name || name;

    // --- name + meta ---
    if (charpanelName) charpanelName.textContent = currentCharName;
    const metaParts = [];
    if (info.occupation) metaParts.push(info.occupation);
    if (info.age) metaParts.push(`Age ${info.age}`);
    if (charpanelMeta) charpanelMeta.textContent = metaParts.join(' · ');

    // --- description: a dedicated "description" field takes priority
    //     (user-editable via the pencil icon); falls back to the
    //     character's backstory/appearance if none has been set yet ---
    setDescriptionText(info.description || info.backstory || info.appearance || '');
    if (charpanelDescEditBtn) charpanelDescEditBtn.classList.remove('hidden');

    // --- images ---
    await refreshCharpanelGallery();
  } catch (err) {
    console.error('loadCharacterPanel failed:', err);
    renderCharpanelEmpty();
  }
}

function setDescriptionText(desc) {
  if (charpanelDesc) {
    charpanelDesc.textContent = desc;
    charpanelDesc.classList.remove('expanded');
  }
  if (charpanelSeeMore) {
    charpanelSeeMore.textContent = 'See more';
    charpanelSeeMore.classList.toggle('hidden', desc.length < 180);
  }
}

/* --- IMAGE GALLERY --- */

async function refreshCharpanelGallery() {
  if (!currentCharId) return;
  try {
    const res = await fetch(`/character_gallery/${encodeURIComponent(currentCharId)}`);
    const payload = res.ok ? await res.json() : { images: [] };
    charpanelImages = (payload.images || []).map(img => ({
      url: `${img.url}${img.url.includes('?') ? '&' : '?'}t=${Date.now()}`,
      filename: img.filename
    }));
  } catch (err) {
    console.error('refreshCharpanelGallery failed:', err);
    charpanelImages = [];
  }
  charpanelIndex = Math.min(charpanelIndex, Math.max(charpanelImages.length - 1, 0));
  renderCharpanelGallery();
}

function renderCharpanelGallery() {
  const hasImages = charpanelImages.length > 0;

  if (charpanelEmpty) charpanelEmpty.classList.toggle('hidden', hasImages);
  if (charpanelImage) charpanelImage.classList.toggle('hidden', !hasImages);

  if (hasImages) {
    charpanelImage.src = charpanelImages[charpanelIndex].url;
    charpanelImage.onerror = () => {
      charpanelImages.splice(charpanelIndex, 1);
      charpanelIndex = Math.min(charpanelIndex, Math.max(charpanelImages.length - 1, 0));
      renderCharpanelGallery();
    };
  }

  // Mobile header avatar always mirrors the main avatar (index 0),
  // regardless of which gallery image the desktop panel is showing.
  if (activeBarAvatar) activeBarAvatar.src = hasImages ? charpanelImages[0].url : '';

  const multi = charpanelImages.length > 1;
  if (charpanelCounter) {
    charpanelCounter.classList.toggle('hidden', !hasImages);
    charpanelCounter.textContent = `${charpanelIndex + 1} / ${charpanelImages.length}`;
  }
  if (charpanelPrev) charpanelPrev.classList.toggle('hidden', !multi);
  if (charpanelNext) charpanelNext.classList.toggle('hidden', !multi);

  if (charpanelThumbs) {
    charpanelThumbs.innerHTML = '';
    charpanelThumbs.classList.toggle('hidden', !currentCharId);

    charpanelImages.forEach((img, i) => {
      const thumb = document.createElement('div');
      thumb.className = 'charpanel-thumb' + (i === charpanelIndex ? ' active' : '');
      const thumbImg = document.createElement('img');
      thumbImg.src = img.url;
      thumbImg.alt = '';
      thumb.appendChild(thumbImg);
      thumb.addEventListener('click', () => { charpanelIndex = i; renderCharpanelGallery(); });

      // Only gallery-sourced images (filename set) can be deleted here —
      // the main avatar is managed through the character JSON builder.
      if (img.filename) {
        const del = document.createElement('button');
        del.className = 'charpanel-thumb-delete';
        del.type = 'button';
        del.title = 'Remove image';
        del.textContent = '✕';
        del.addEventListener('click', (e) => { e.stopPropagation(); deleteGalleryImage(img.filename); });
        thumb.appendChild(del);
      }

      charpanelThumbs.appendChild(thumb);
    });

    const addTile = document.createElement('button');
    addTile.type = 'button';
    addTile.className = 'charpanel-thumb-add';
    addTile.title = 'Add image';
    addTile.setAttribute('aria-label', 'Add image');
    addTile.textContent = '+';
    addTile.addEventListener('click', () => {
      if (!currentCharId) return;
      charpanelAddImageInput.click();
    });
    charpanelThumbs.appendChild(addTile);
  }
}

function shiftCharpanelImage(delta) {
  if (charpanelImages.length < 2) return;
  charpanelIndex = (charpanelIndex + delta + charpanelImages.length) % charpanelImages.length;
  renderCharpanelGallery();
}

async function handleAddImageFile() {
  const file = charpanelAddImageInput.files && charpanelAddImageInput.files[0];
  charpanelAddImageInput.value = ''; // reset so picking the same file again still fires 'change'
  if (!file || !currentCharId) return;

  const formData = new FormData();
  formData.append('char_id', currentCharId);
  formData.append('image', file);

  try {
    const res = await fetch('/add_character_image', { method: 'POST', body: formData });
    const result = await res.json();
    if (!res.ok || result.status !== 'success') {
      console.error('add_character_image failed:', result);
      return;
    }
    charpanelIndex = charpanelImages.length; // land on the newly added image
    await refreshCharpanelGallery();
  } catch (err) {
    console.error('handleAddImageFile failed:', err);
  }
}

async function deleteGalleryImage(filename) {
  if (!currentCharId) return;
  try {
    const res = await fetch('/delete_character_image', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ char_id: currentCharId, filename })
    });
    const result = await res.json();
    if (!res.ok || result.status !== 'success') {
      console.error('delete_character_image failed:', result);
      return;
    }
    await refreshCharpanelGallery();
  } catch (err) {
    console.error('deleteGalleryImage failed:', err);
  }
}

/* --- DESCRIPTION EDITING --- */

function openDescEdit() {
  if (!charpanelDescEditBox || !currentCharId) return;
  charpanelDescTextarea.value = charpanelDesc.textContent || '';
  charpanelDescEditBox.classList.remove('hidden');
  charpanelDescTextarea.focus();
}

function closeDescEdit() {
  if (charpanelDescEditBox) charpanelDescEditBox.classList.add('hidden');
}

async function saveDescEdit() {
  if (!currentCharId) return;
  const newDesc = charpanelDescTextarea.value.trim();

  const formData = new FormData();
  formData.append('char_id', currentCharId);
  formData.append('data', JSON.stringify({ description: newDesc }));

  try {
    const res = await fetch('/edit_character', { method: 'POST', body: formData });
    const result = await res.json();
    if (!res.ok || result.status !== 'success') {
      console.error('saveDescEdit failed:', result);
      return;
    }
    setDescriptionText(newDesc);
    closeDescEdit();
  } catch (err) {
    console.error('saveDescEdit failed:', err);
  }
}

/* =============================================
   HELPER: removeLastExchange()
   Removes last user+bot pair from DOM on undo
   ============================================= */
function removeLastExchange() {
  const children = Array.from(chatContainer.children);
  const toRemove = [];
  let state = 'find-separator';

  for (let i = children.length - 1; i >= 0; i--) {
    const el = children[i];
    if (state === 'find-separator') {
      if (el.classList.contains('msg-separator'))  { toRemove.push(el); state = 'find-bot'; }
    } else if (state === 'find-bot') {
      if (el.classList.contains('bot-row'))        { toRemove.push(el); state = 'find-spacer'; }
    } else if (state === 'find-spacer') {
      if (el.classList.contains('msg-spacer'))     { toRemove.push(el); state = 'find-user'; }
    } else if (state === 'find-user') {
      if (el.classList.contains('user-row'))       { toRemove.push(el); break; }
    }
  }

  toRemove.forEach(el => el.remove());
}


/* =============================================
   CORE: streamBotReply
   Reads the raw SSE stream and updates the bubble.
   ============================================= */
async function streamBotReply(userText, targetEl, cursor) {
  const character = charSelect.value;
  const model     = modelSelect.value;

  let accumulatedText = '';
  abortController     = new AbortController();

  try {
    const response = await fetch('/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: userText, character, model }),
      signal:  abortController.signal
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split('\n\n');
      buffer = chunks.pop(); // Keep the incomplete chunk in the buffer

      for (const chunk of chunks) {
        const lines = chunk.split('\n');
        let eventType = 'message';
        let dataPayload = '';

        // Ultra-fast text parser
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            // FIX 1: Safely remove only the SSE protocol space, preserving token spaces
            dataPayload = line.slice(5).replace(/^ /, '').replace(/\\n/g, '\n');
          }
        }
        if (eventType === 'chunk') {

          // FIX 2: Use += to glue the incoming tokens together
          accumulatedText     += dataPayload;
          targetEl.textContent = accumulatedText;
          targetEl.appendChild(cursor);

          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();

        } else if (eventType === 'done') {
          const finalText = (dataPayload && dataPayload.trim()) ? dataPayload : accumulatedText || '...';
          targetEl.classList.remove('streaming');
          targetEl.innerHTML = '';
          targetEl.appendChild(parseActionText(finalText));

          const separator = document.createElement('hr');
          separator.classList.add('msg-separator');
          chatContainer.appendChild(separator);

          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();
          return;

        } else if (eventType === 'error') {
          targetEl.classList.remove('streaming');
          targetEl.innerHTML   = '';
          targetEl.classList.add('msg-error');
          targetEl.textContent = '⚠️ ' + (dataPayload || 'Unknown error.');

          const separator = document.createElement('hr');
          separator.classList.add('msg-separator');
          chatContainer.appendChild(separator);

          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();
          return;
        }
      }
    }
  } catch (err) {
    targetEl.classList.remove('streaming');
    targetEl.innerHTML = '';

    if (err.name === 'AbortError') {
      targetEl.appendChild(parseActionText(accumulatedText));
    } else {
      targetEl.classList.add('msg-error');
      targetEl.textContent = '⚠️ Connection error: ' + err.message;
    }

    const separator = document.createElement('hr');
    separator.classList.add('msg-separator');
    chatContainer.appendChild(separator);

    if (typeof optimizedScroll === 'function') optimizedScroll();
    else scrollToBottom();
  }
}

/* =============================================
   HELPER: streamAction()
   Handles streaming SSE responses for Utility buttons
   like Retry and Continue.
   ============================================= */
/* =============================================
   HELPER: streamAction()
   Handles streaming SSE responses for Utility buttons.
   ============================================= */
async function streamAction(endpoint, payloadData, targetEl, cursor, parentEl = null) {
  let accumulatedText = '';
  abortController     = new AbortController();

  try {
    const response = await fetch(endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payloadData),
      signal:  abortController.signal
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split('\n\n');
      buffer = chunks.pop();

      for (const chunk of chunks) {
        const lines = chunk.split('\n');
        let eventType = 'message';
        let dataPayload = '';

        // Ultra-fast text parser
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            // FIX 1: Safely remove only the SSE protocol space, preserving token spaces
            dataPayload = line.slice(5).replace(/^ /, '').replace(/\\n/g, '\n');
          }
        }

        if (eventType === 'chunk') {
          // Remove Ghost Action illusion
          const ghost = targetEl.querySelector('.ghost-text');
          if (ghost) ghost.remove();

          // FIX 2: Use += to glue the incoming tokens together
          accumulatedText     += dataPayload;
          targetEl.textContent = accumulatedText;

          if (cursor && parentEl) parentEl.appendChild(cursor);
          else if (cursor) targetEl.appendChild(cursor);

          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();

        } else if (eventType === 'done') {
          const finalText = (dataPayload && dataPayload.trim()) ? dataPayload : accumulatedText || '...';
          targetEl.innerHTML = '';
          targetEl.appendChild(parseActionText(finalText));

          if (parentEl) parentEl.classList.remove('streaming');
          else targetEl.classList.remove('streaming');
          if (cursor) cursor.remove();

          if (endpoint === '/retry') {
            const separator = document.createElement('hr');
            separator.classList.add('msg-separator');
            chatContainer.appendChild(separator);
          }
          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();
          return;

        } else if (eventType === 'error') {
          targetEl.innerHTML   = '';
          targetEl.classList.add('msg-error');
          targetEl.textContent = '⚠️ ' + (dataPayload || 'Unknown error.');

          if (parentEl) parentEl.classList.remove('streaming');
          else targetEl.classList.remove('streaming');
          if (cursor) cursor.remove();

          if (endpoint === '/retry') {
            const separator = document.createElement('hr');
            separator.classList.add('msg-separator');
            chatContainer.appendChild(separator);
          }
          if (typeof optimizedScroll === 'function') optimizedScroll();
          else scrollToBottom();
          return;
        }
      }
    }
  } catch (err) {
    if (parentEl) parentEl.classList.remove('streaming');
    else targetEl.classList.remove('streaming');
    if (cursor) cursor.remove();

    if (err.name === 'AbortError') {
      targetEl.innerHTML = '';
      targetEl.appendChild(parseActionText(accumulatedText + ' [Stopped]'));
      if (endpoint === '/retry') {
        const separator = document.createElement('hr');
        separator.classList.add('msg-separator');
        chatContainer.appendChild(separator);
      }
    } else {
      targetEl.classList.add('msg-error');
      targetEl.textContent = '⚠️ Connection error: ' + err.message;
    }

    if (typeof optimizedScroll === 'function') optimizedScroll();
    else scrollToBottom();
  }
}
/* =============================================
   BACKEND: getBotReply — kept for retry/continue
   which still use JSON (not streaming)
   ============================================= */
function getBotReply(userText) {
  // NOTE: This is only used internally now.
  // sendMessage() uses streamBotReply() instead.
  // Retry and Continue have their own fetch calls.
  const character = charSelect.value;
  const model     = modelSelect.value;

  return fetch('/chat', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ message: userText, character, model })
  })
  .then(res => res.json())
  .then(data => {
    if (data.error) throw new Error(data.error);
    return data.reply;
  });
}

/* =============================================
   SETTINGS MODAL
   ============================================= */
function openSettingsModal() {
  const character = charSelect.value;
  const model     = modelSelect.value;

  if (!character) {
    addSystemMessage('⚠️ Please select a character first.');
    return;
  }

  fetch('/settings', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ character, model })
  })
  .then(r => r.json())
  .then(data => {
    document.getElementById('s-model').textContent      = data.active_model     || '—';
    document.getElementById('s-connection').textContent = data.connection        || '—';
    document.getElementById('s-mode').textContent       = data.story_mode        || '—';
    document.getElementById('s-relmode').textContent    = data.relationship_mode || '—';
    document.getElementById('s-temp').textContent       = data.temperature       ?? '—';
    document.getElementById('s-topp').textContent       = data.top_p             ?? '—';
    document.getElementById('s-maxtok').textContent     = data.max_tokens        ?? '—';
    document.getElementById('s-rep').textContent        = data.rep_penalty       ?? '—';
    document.getElementById('s-tokens').textContent     = data.estimated_tokens  ?? '—';
    // Sync the thoughts toggle to current backend state
    setThoughtsUI(data.show_thoughts === true);
    settingsModal.classList.remove('hidden');
  })
  .catch(() => {
    addSystemMessage('⚠️ Could not load settings — is the server running?');
  });
}

function closeSettingsModal() {
  settingsModal.classList.add('hidden');
}

/* =============================================
   CHAT MANAGEMENT
   ============================================= */
function handleNewChat() {
  const character = charSelect.value;
  if (!character) return;

  fetch('/new_chat', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ character })
  })
  .then(r => r.json())
  .then(data => {
    backend_current_chat_id = data.chat_id; // <-- ADD THIS LINE
    backend_current_path    = data.path || null;
    chatContainer.innerHTML = '';
    if (data.first_message) {
      addBotMessage(data.first_message);
    } else {
      addSystemMessage('✨ New chat initialized. Send a message to begin!');
    }
    refreshChatList();
  })
  .catch(() => {
    chatContainer.innerHTML = '';
    addSystemMessage('✨ New chat initialized. Send a message to begin!');
  });
}

function showConfirmModal() {
  confirmModal.classList.remove('hidden');
}

function hideConfirmModal() {
  confirmModal.classList.add('hidden');
}

function handleDeleteConfirmed() {
  hideConfirmModal();

  if (backend_current_path) {
    fetch('/delete_chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        path: backend_current_path,
        character: charSelect.value,            // <-- ADD THIS
        chat_id: backend_current_chat_id
      })
    })
    .then(() => {
      backend_current_path    = null;
      backend_current_chat_id = null;
      chatContainer.innerHTML = '';
      addSystemMessage('🗑️ Chat deleted. Start a new conversation.');
      refreshChatList();
      userInput.focus();
    })
    .catch(() => {
      chatContainer.innerHTML = '';
      addSystemMessage('🗑️ Chat cleared.');
      userInput.focus();
    });
  } else {
    chatContainer.innerHTML = '';
    addSystemMessage('🗑️ Chat cleared.');
    userInput.focus();
  }
}

/* =============================================
   AI CHARACTER WRITER
   Vibe → backend generates, saves, and activates
   a full character card via /create_character.
   ============================================= */
const CHAR_WRITER_STATUS_MESSAGES = [
  'Drafting character backstory...',
  'Shaping their personality...',
  'Writing their opening line...',
  'Polishing the roleplay rules...'
];

const CHAR_WRITER_MAX_LEN = 4000;

function setCharWriterBusy(busy) {
  charWriterGenerateBtn.disabled = busy;
  charWriterCancelBtn.disabled   = busy;
  charWriterInput.disabled       = busy;
  charWriterSpinner.classList.toggle('hidden', !busy);
}

function updateCharWriterCounter() {
  const len = charWriterInput.value.length;
  charWriterCounter.textContent = `${len} / ${CHAR_WRITER_MAX_LEN}`;
  charWriterCounter.classList.toggle('near-limit', len > CHAR_WRITER_MAX_LEN * 0.9);
}

function openCharWriterModal() {
  charWriterModal.classList.remove('hidden');
  charWriterInput.value = '';
  charWriterStatus.textContent = '';
  charWriterStatus.classList.remove('error');
  updateCharWriterCounter();
  setCharWriterBusy(false);
  charWriterInput.focus();
}

function closeCharWriterModal() {
  if (charWriterStatusInterval) {
    clearInterval(charWriterStatusInterval);
    charWriterStatusInterval = null;
  }
  charWriterModal.classList.add('hidden');
  setCharWriterBusy(false);
}

/* Rebuilds the character <select> from scratch (avoids duplicate <option>s
   on repeated refreshes) and optionally selects the freshly created one. */
function refreshCharacterDropdown(characters, selected) {
  charSelect.innerHTML = '<option value="">Select Character</option>';
  characters.forEach(char => {
    const opt = document.createElement('option');
    opt.value       = char;
    opt.textContent = char;
    charSelect.appendChild(opt);
  });
  if (selected) charSelect.value = selected;
}

function generateCharacter() {
  const vibe = charWriterInput.value.trim();
  if (!vibe) {
    charWriterStatus.textContent = 'Please describe a character idea first.';
    charWriterStatus.classList.add('error');
    return;
  }

  setCharWriterBusy(true);
  charWriterStatus.classList.remove('error');

  let msgIndex = 0;
  charWriterStatus.textContent = CHAR_WRITER_STATUS_MESSAGES[0];
  charWriterStatusInterval = setInterval(() => {
    msgIndex = (msgIndex + 1) % CHAR_WRITER_STATUS_MESSAGES.length;
    charWriterStatus.textContent = CHAR_WRITER_STATUS_MESSAGES[msgIndex];
  }, 2200);

  fetch('/create_character', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ vibe })
  })
    .then(r => r.json().then(data => ({ ok: r.ok, data })))
    .then(({ ok, data }) => {
      if (charWriterStatusInterval) {
        clearInterval(charWriterStatusInterval);
        charWriterStatusInterval = null;
      }

      if (!ok || data.status !== 'ok') {
        charWriterStatus.textContent = '⚠️ ' + (data.error || 'Character generation failed.');
        charWriterStatus.classList.add('error');
        setCharWriterBusy(false);
        return;
      }

      // Refresh the dropdown and switch to the new character
      refreshCharacterDropdown(data.characters, data.character);
      backend_current_chat_id = data.chat_id || null;
      backend_current_path    = data.path || null;
      updateActiveBar();
      refreshChatList();

      // Show the new character's greeting in the chat box
      chatContainer.innerHTML = '';
      addSystemMessage(`✨ Created "${data.character}" and switched the active chat.`);
      if (data.greeting) addBotMessage(data.greeting);

      closeCharWriterModal();
      userInput.focus();
    })
    .catch(err => {
      if (charWriterStatusInterval) {
        clearInterval(charWriterStatusInterval);
        charWriterStatusInterval = null;
      }
      charWriterStatus.textContent = '⚠️ Connection error: ' + err.message;
      charWriterStatus.classList.add('error');
      setCharWriterBusy(false);
    });
}

/* =============================================
   EVENT LISTENERS
   ============================================= */
function attachEventListeners() {
  // --- CHARACTER MANAGE BUTTON ---
  if (charManageBtn) {
    charManageBtn.addEventListener('click', () => {
      // Opens the json_builder.html tool in a new tab
      window.open('json_builder.html', '_blank');
    });
  }
  // --- SCROLL TO BOTTOM ---
  if (chatDisplay) {
    chatDisplay.addEventListener('scroll', handleChatScroll);
  }

  if (scrollBottomBtn) {
    scrollBottomBtn.addEventListener('click', () => {
      chatDisplay.scrollTo({
        top: chatDisplay.scrollHeight,
        behavior: 'smooth' // Provides a nice scrolling animation on click
      });
    });
  }
  // --- AI CHARACTER WRITER ---
  if (aiCharWriterBtn) aiCharWriterBtn.addEventListener('click', openCharWriterModal);
  charWriterClose.addEventListener('click', closeCharWriterModal);
  charWriterCancelBtn.addEventListener('click', closeCharWriterModal);
  charWriterGenerateBtn.addEventListener('click', generateCharacter);
  charWriterModal.addEventListener('click', e => {
    if (e.target === charWriterModal) closeCharWriterModal();
  });
  charWriterInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      generateCharacter();
    }
  });
  charWriterInput.addEventListener('input', updateCharWriterCounter);

  // --- LOGIN ---
  loginBtn.addEventListener('click', checkPassword);
  pwInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') checkPassword();
  });

  // --- SEND ---
  sendBtn.addEventListener('click', sendMessage);
  userInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // --- UTIL BAR ---
  document.getElementById('util-bar').addEventListener('click', e => {
    const btn = e.target.closest('.util-btn');
    if (!btn) return;

    const action = btn.dataset.action;

    // 1. HANDLE STOP IMMEDIATELY (Bypass the isSending guard)
    if (action === 'stop') {
      if (isSending && abortController) {
        abortController.abort(); // Instantly severs the fetch request
        // addSystemMessage('■ Generation stopped by user.');
      }

      // Force unlock the UI immediately
      isSending           = false;
      sendBtn.disabled    = false;
      charSelect.disabled = false;
      modelSelect.disabled = false;
      showTyping(false);
      return;
    }

    // 2. HARDWARE GUARD: Prevent concurrent LLM requests
    if (isSending) {
      addSystemMessage('⚠️ Please wait for the current generation to finish.');
      return;
    }

    const character = charSelect.value;
    const model     = modelSelect.value;

    // Lock the UI for utility actions
    if (['retry', 'continue', 'scene', 'impersonate'].includes(action)) {
      isSending = true;
    }

   // RETRY
    if (action === 'retry') {
      if (!character) return addSystemMessage('⚠️ Select a character first.');

      // 1. Instantly remove old message from the DOM
      const lastBot = [...chatContainer.querySelectorAll('.bot-row')].pop();
      const lastSep = lastBot?.nextElementSibling;
      if (lastBot) lastBot.remove();
      if (lastSep?.classList.contains('msg-separator')) lastSep.remove();

      showTyping(true);

      // 2. Create fresh target bubble with blinking cursor
      const botRow = document.createElement('div');
      botRow.classList.add('message-row', 'bot-row');

      const botAvatar = document.createElement('div');
      botAvatar.classList.add('avatar');
      botAvatar.textContent = '✨';

      const botMsgContent = document.createElement('div');
      botMsgContent.classList.add('msg-content');

      const botMsgText = document.createElement('div');
      botMsgText.classList.add('msg-text', 'streaming');

      const cursor = document.createElement('span');
      cursor.classList.add('stream-cursor');
      cursor.textContent = '▍';

      botMsgText.appendChild(cursor);
      botMsgContent.appendChild(botMsgText);
      botRow.appendChild(botAvatar);
      botRow.appendChild(botMsgContent);
      attachMessageActions(botRow, botMsgText, botMsgContent);
      chatContainer.appendChild(botRow);
      scrollToBottom();

      // 3. Stream the retry
      streamAction('/retry', { character, model }, botMsgText, cursor)
        .finally(() => {
          showTyping(false);
          isSending = false;
        });

    }

    // UNDO
    if (action === 'undo') {
      fetch('/undo', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ character })
      })
      .then(r => r.json())
      .then(data => {
        if (data.error) return addSystemMessage('⚠️ Undo error: ' + data.error);
        removeLastExchange();
        // Restore the undone user message into the input box for editing
        const restored = (data.removed_user_message || '').trim();
        if (restored) {
          userInput.value = restored;
          userInput.focus();
          userInput.setSelectionRange(restored.length, restored.length);
        }
      })
      .catch(() => addSystemMessage('⚠️ Undo failed.'));
    }

    // CONTINUE
    if (action === 'continue') {
      if (!character) return addSystemMessage('⚠️ Select a character first.');

      const lastBotText = [...chatContainer.querySelectorAll('.bot-row .msg-text')].pop();
      if (!lastBotText) return addSystemMessage('⚠️ No message to continue.');

      showTyping(true);
      lastBotText.classList.add('streaming');

      // Add a space and a specific span just for the newly streaming text
      lastBotText.appendChild(document.createTextNode(' '));
      const newTextSpan = document.createElement('span');
      lastBotText.appendChild(newTextSpan);

      const cursor = document.createElement('span');
      cursor.classList.add('stream-cursor');
      cursor.textContent = '▍';
      lastBotText.appendChild(cursor);

      // 3. Stream the continuation
      streamAction('/continue', { character, model }, newTextSpan, cursor, lastBotText)
        .finally(() => {
          showTyping(false);
          isSending = false;
        });
    }

    // SCENE
    if (action === 'scene') {
      if (!character) return addSystemMessage('⚠️ Select a character first.');
      addSystemMessage('🎬 Generating scene image...');
      fetch('/scene', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ character, model })
      })
      .then(r => r.json())
      .then(data => {
        if (data.error) return addSystemMessage('⚠️ Scene error: ' + data.error);
        if (data.image_url) {
          addSceneImage(data.image_url, data.text || 'Generated scene');
        } else {
          addSystemMessage('🎬 ' + (data.text || 'No scene image generated.'));
        }
      })
      .catch(() => addSystemMessage('⚠️ Scene generation failed.'));
    }

    // IMPERSONATE
    if (action === 'impersonate') {
      if (!character) return addSystemMessage('⚠️ Select a character first.');
      addSystemMessage('🤖 Generating suggested message...');
      fetch('/impersonate', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ character, model })
      })
      .then(r => r.json())
      .then(data => {
        if (data.error) return addSystemMessage('⚠️ Impersonate error: ' + data.error);
        userInput.value = data.suggested || '';
        userInput.focus();
        // Remove the "Generating..." status message
        const sysMsgs = chatContainer.querySelectorAll('.system-message');
        sysMsgs[sysMsgs.length - 1]?.remove();
      })
      .catch(() => addSystemMessage('⚠️ Impersonate failed.'))
      .finally(() => isSending = false);
    }
  });
  // --- CHARACTER CHAT DROPDOWN ---
  if (charChatToggle && charChatDropdown) {
    charChatToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = charChatDropdown.classList.toggle('hidden');

      if (!isHidden) {
        const character = charSelect.value; 

        if (!character || character === '—') {
          charChatDropdown.innerHTML = `<div style="padding: 10px; color: var(--text-muted); font-size: 12px; text-align: center;">No character selected</div>`;
          return;
        }

        charChatDropdown.innerHTML = `<div style="padding: 10px; color: var(--text-muted); font-size: 12px; text-align: center;">Loading chats...</div>`;

        // Fetch specifically filtered list via backend route
        fetch('/chat_list', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ character: character })
        })
        .then(r => r.json())
        .then(chats => {
          charChatDropdown.innerHTML = "";
          if (!chats || chats.length === 0) {
            charChatDropdown.innerHTML = `<div style="padding: 10px; color: var(--text-muted); font-size: 12px; text-align: center;">No chat history found</div>`;
            return;
          }

          // Use your existing formatChatDate and escapeHtml utilities
          chats.forEach(chat => {
            const btn = document.createElement("button");
            btn.className = "char-chat-dropdown-item";
            const dateStr = chat.last_activity ? formatChatDate(chat.last_activity) : "";

            btn.innerHTML = `
              <strong>${escapeHtml(chat.title || "Chat " + chat.chat_id)}</strong>
              <span class="chat-date">${escapeHtml(chat.preview || "No messages yet")} • ${dateStr}</span>
            `;

            btn.addEventListener("click", () => {
              // Loads the selected chat without refreshing the page
              loadChat(chat.path); 
              charChatDropdown.classList.add("hidden");
            });

            charChatDropdown.appendChild(btn);
          });
        })
        .catch(() => {
          charChatDropdown.innerHTML = `<div style="padding: 10px; color: #E5675F; font-size: 12px; text-align: center;">Failed to load chats</div>`;
        });
      }
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
      if (!charChatToggle.contains(e.target)) {
        charChatDropdown.classList.add('hidden');
      }
    });
  }
  // --- SIDEBAR CONTROLS ---
  newChatBtn.addEventListener('click', handleNewChat);
  delChatBtn.addEventListener('click', showConfirmModal);

  // --- DELETE MODAL ---
  modalConfirm.addEventListener('click', handleDeleteConfirmed);
  modalCancel.addEventListener('click', hideConfirmModal);
  confirmModal.addEventListener('click', e => {
    if (e.target === confirmModal) hideConfirmModal();
  });

  // --- SETTINGS MODAL ---
  settingsBtn.addEventListener('click', openSettingsModal);
  settingsClose.addEventListener('click', closeSettingsModal);
  settingsModal.addEventListener('click', e => {
    if (e.target === settingsModal) closeSettingsModal();
  });

  // --- THOUGHTS TOGGLE ---
  thoughtsToggle.addEventListener('click', handleThoughtsToggle);

  // --- SHARED SIDEBAR NAV ---
  // Characters/Create/Edit hand off to home.html's views — character
  // selection lives there now, not in a dropdown on this page.
  document.getElementById('nav-characters').addEventListener('click', () => {
    window.location.href = 'home.html?view=characters';
  });
  document.getElementById('nav-create').addEventListener('click', () => {
    window.location.href = 'home.html?view=create';
  });
  document.getElementById('nav-edit-character').addEventListener('click', () => {
    window.location.href = 'home.html?view=edit-character';
  });

  // "Chats" opens the overlay drawer. It's an overlay (not inline
  // sidebar content) specifically so the persistent sidebar's size
  // never changes when it's open.
  navChatsBtn.addEventListener('click', () => {
    if (chatsDrawer.classList.contains('hidden')) openChatsDrawer();
    else closeChatsDrawer();
  });
  chatsDrawerCloseBtn.addEventListener('click', closeChatsDrawer);
  chatsDrawerBackdrop.addEventListener('click', closeChatsDrawer);

  // "⋮" button (mobile only, hidden via CSS on desktop) — opens the
  // Model selector as a dropdown instead of it sitting inline in the bar.
  const activeBarMenuBtn   = document.getElementById('active-bar-menu-btn');
  const activeBarRightMenu = document.getElementById('active-bar-right');
  if (activeBarMenuBtn && activeBarRightMenu) {
    activeBarMenuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = activeBarRightMenu.classList.toggle('menu-open');
      activeBarMenuBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });
    document.addEventListener('click', (e) => {
      if (!activeBarRightMenu.classList.contains('menu-open')) return;
      if (activeBarRightMenu.contains(e.target) || activeBarMenuBtn.contains(e.target)) return;
      activeBarRightMenu.classList.remove('menu-open');
      activeBarMenuBtn.setAttribute('aria-expanded', 'false');
    });
  }

  // --- CHATS SEARCH / EDIT MODE / BULK DELETE (mirrors home.html) ---
  chatsSearchInput.addEventListener('input', () => {
    renderChatsList(ALL_CHATS.filter(matchesChatsSearch));
  });

  chatsEditToggle.addEventListener('click', () => {
    if (chatsEditMode) exitChatsEditMode(); else enterChatsEditMode();
  });
  chatsEditbarDone.addEventListener('click', exitChatsEditMode);

  chatsDeleteBtn.addEventListener('click', () => {
    if (selectedChats.size === 0) return;
    const count = selectedChats.size;
    if (!confirm(`Delete ${count} chat${count > 1 ? 's' : ''}? This can't be undone.`)) return;

    const toDelete = Array.from(selectedChats.values());
    const deletingCurrentChat = toDelete.some(c => c.path === backend_current_path);

    Promise.all(toDelete.map(chat =>
      fetch('/delete_chat', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ path: chat.path, character: chat.character, chat_id: chat.chat_id })
      }).catch(() => null)
    )).then(() => {
      exitChatsEditMode();
      refreshChatList();
      // If the chat currently open in the main window was among the
      // deleted ones, don't leave a dead conversation on screen.
      if (deletingCurrentChat) handleNewChat();
    });
  });

  // --- CHARACTER CHANGE ---
  charSelect.addEventListener('change', () => {
    refreshChatList();
    updateActiveBar();
    chatContainer.innerHTML = '';
    handleNewChat(); // <-- ADD THIS: Forces backend to clear RAM and start fresh
  });

  // --- MODEL CHANGE ---
  modelSelect.addEventListener('change', () => {
    updateActiveBar();
    triggerWarmUp();
  });
}

/* =============================================
   THOUGHTS TOGGLE
   Mirrors backend.show_thoughts = True/False
   ============================================= */
function setThoughtsUI(enabled) {
  thoughtsToggle.setAttribute('aria-pressed', enabled ? 'true' : 'false');
  thoughtsToggle.classList.toggle('toggle-on', enabled);
  thoughtsLabel.textContent = enabled ? 'On' : 'Off';
}

function handleThoughtsToggle() {
  const current = thoughtsToggle.getAttribute('aria-pressed') === 'true';
  const next    = !current;
  fetch('/toggle_thoughts', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ enable: next })
  })
  .then(r => r.json())
  .then(data => setThoughtsUI(data.show_thoughts))
  .catch(() => addSystemMessage('⚠️ Could not toggle thoughts.'));
}

/* =============================================
   ACTIVITY LOG PANEL
   Polls /logs_poll every few seconds — only while the
   panel is open — instead of holding a permanent connection.
   ============================================= */

function startLogPolling() {
  if (logPollTimer) return; // already running
  pollLogsOnce(); // immediate fetch so it doesn't feel laggy on open
  logPollTimer = setInterval(pollLogsOnce, 3000);
  logStatus.textContent = '● Connected';
  logStatus.style.color = '#e4e4e5';
}

function stopLogPolling() {
  if (logPollTimer) {
    clearInterval(logPollTimer);
    logPollTimer = null;
  }
}

function pollLogsOnce() {
  fetch(`/logs_poll?since=${logLastId}`)
    .then(r => r.json())
    .then(data => {
      if (data.lines && data.lines.length) {
        data.lines.forEach(l => appendLogLine(l.text));
        logLastId = data.latest_id;
      }
    })
    .catch(() => {
      logStatus.textContent = '● Disconnected — retrying...';
      logStatus.style.color = '#666';
    });
}

function appendLogLine(text) {
  const line = document.createElement('div');
  line.classList.add('log-line');

  // Colour-code by prefix
  if (text.includes('[⚠️') || text.includes('Error') || text.includes('error')) {
    line.classList.add('log-warn');
  } else if (text.includes('[✅') || text.includes('[🧠') || text.includes('[🔄')) {
    line.classList.add('log-ok');
  } else if (text.includes('[🗑️') || text.includes('[🎬') || text.includes('[⚙️')) {
    line.classList.add('log-info');
  }

  line.textContent = text;
  logOutput.appendChild(line);

  // STUTTER FIX: Only clean up the DOM when it gets excessively large,
  // and do it in batches to prevent UI freezing while the model streams.
  if (logOutput.children.length > 350) {
    while (logOutput.children.length > 300) {
      logOutput.removeChild(logOutput.firstChild);
    }
  }

  // Auto-scroll to bottom

  // Auto-scroll to bottom
  logOutput.scrollTop = logOutput.scrollHeight;

  // Badge counter when panel is closed
  if (!logPanelOpen) {
    logUnreadCount++;
    logBadge.textContent = logUnreadCount > 99 ? '99+' : logUnreadCount;
    logBadge.classList.remove('hidden');
  }
}

function toggleLogPanel() {
  if (logPanelOpen) {
    closeLogPanel();
  } else {
    openLogPanel();
  }
}

function openLogPanel() {
  logPanelOpen = true;
  logPanel.classList.remove('hidden');
  logUnreadCount = 0;
  logBadge.classList.add('hidden');
  logBadge.textContent = '0';
  logOutput.scrollTop = logOutput.scrollHeight;
  startLogPolling();
}

function closeLogPanel() {
  logPanelOpen = false;
  logPanel.classList.add('hidden');
  stopLogPolling();
}

function clearLog() {
  logOutput.innerHTML = '';
  logUnreadCount = 0;
  logBadge.classList.add('hidden');
}

// Makes an element draggable via a handle element
function makeDraggable(el, handle) {
  let startX, startY, startLeft, startTop;

  handle.addEventListener('mousedown', e => {
    startX    = e.clientX;
    startY    = e.clientY;
    const rect = el.getBoundingClientRect();
    startLeft = rect.left;
    startTop  = rect.top;

    // Switch from bottom/right anchoring to top/left for drag
    el.style.right  = 'auto';
    el.style.bottom = 'auto';
    el.style.left   = startLeft + 'px';
    el.style.top    = startTop  + 'px';

    function onMove(e) {
      el.style.left = (startLeft + e.clientX - startX) + 'px';
      el.style.top  = (startTop  + e.clientY - startY) + 'px';
    }

    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
    e.preventDefault();
  });
}

/* =============================================
   BOOT
   ============================================= */
document.addEventListener('DOMContentLoaded', init);
