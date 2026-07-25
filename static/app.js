const testButton = document.querySelector("#test-login");
const testResult = document.querySelector("#test-login-result");

if (testButton && testResult) {
  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    testResult.textContent = "测试中...";
    try {
      const response = await fetch("/api/test-login", { method: "POST" });
      const data = await response.json();
      testResult.textContent = data.message;
      testResult.style.color = data.ok ? "#047857" : "#b91c1c";
    } catch (error) {
      testResult.textContent = `请求失败：${error}`;
      testResult.style.color = "#b91c1c";
    } finally {
      testButton.disabled = false;
    }
  });
}

const picker = document.querySelector("[data-read-num-picker]");
const readNumInput = document.querySelector("input[name='READ_NUM']");

if (picker && readNumInput) {
  picker.addEventListener("click", (event) => {
    const button = event.target.closest("[data-read-num]");
    if (!button) return;
    readNumInput.value = button.dataset.readNum;
    picker.querySelectorAll("[data-read-num]").forEach((item) => {
      item.classList.toggle("selected", item === button);
    });
  });
}

const bookSearchInput = document.querySelector("#book-search-input");
const bookSearchButton = document.querySelector("#book-search-button");
const bookSearchStatus = document.querySelector("#book-search-status");
const bookSearchResults = document.querySelector("#book-search-results");
const selectedBooksList = document.querySelector("#selected-books-list");

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => {
    const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return entities[char];
  });
}

function selectedBookIds() {
  if (!selectedBooksList) return new Set();
  return new Set(
    Array.from(selectedBooksList.querySelectorAll("[data-selected-book]")).map(
      (item) => item.dataset.bookId,
    ),
  );
}

function renderBookCard(book, selected = false) {
  const bookId = escapeHtml(book.bookId);
  const title = escapeHtml(book.title || book.bookId);
  const author = escapeHtml(book.author || "");
  const cover = escapeHtml(book.cover || "");
  const button = selected
    ? '<button type="button" class="ghost-button" data-remove-book>移除</button>'
    : '<button type="button" data-add-book>添加</button>';
  const hiddenInputs = selected
    ? `
      <input type="hidden" name="SELECTED_BOOKS" value="${bookId}">
      <input type="hidden" name="BOOK_ID" value="${bookId}">
      <input type="hidden" name="BOOK_TITLE" value="${title}">
      <input type="hidden" name="BOOK_AUTHOR" value="${author}">
      <input type="hidden" name="BOOK_COVER" value="${cover}">
    `
    : "";
  return `
    <div class="book-card" data-${selected ? "selected-" : ""}book data-book-id="${bookId}" data-title="${title}" data-author="${author}" data-cover="${cover}">
      ${cover ? `<img src="${cover}" alt="${title}">` : '<div class="book-cover-placeholder"></div>'}
      <div>
        <strong>${title}</strong>
        <span>${author || bookId}</span>
      </div>
      ${button}
      ${hiddenInputs}
    </div>
  `;
}

async function searchBooks() {
  const keyword = bookSearchInput.value.trim();
  if (!keyword) {
    bookSearchStatus.textContent = "请输入书名或作者";
    return;
  }
  bookSearchButton.disabled = true;
  bookSearchStatus.textContent = "正在搜索书城...";
  bookSearchResults.innerHTML = "";
  try {
    const response = await fetch(`/api/books/search?q=${encodeURIComponent(keyword)}`);
    const data = await response.json();
    if (!data.ok) throw new Error(data.message || "搜索失败");
    const picked = selectedBookIds();
    const books = data.books.filter((book) => !picked.has(book.bookId));
    bookSearchStatus.textContent = books.length ? `找到 ${books.length} 本` : "没有新的可添加书目";
    bookSearchResults.innerHTML = books.map((book) => renderBookCard(book)).join("");
  } catch (error) {
    bookSearchStatus.textContent = `搜索失败：${error.message || error}`;
  } finally {
    bookSearchButton.disabled = false;
  }
}

if (bookSearchInput && bookSearchButton && bookSearchResults && selectedBooksList) {
  bookSearchButton.addEventListener("click", searchBooks);
  bookSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchBooks();
    }
  });

  bookSearchResults.addEventListener("click", (event) => {
    const button = event.target.closest("[data-add-book]");
    if (!button) return;
    const card = button.closest("[data-book]");
    if (!card || selectedBookIds().has(card.dataset.bookId)) return;
    selectedBooksList.insertAdjacentHTML(
      "beforeend",
      renderBookCard(
        {
          bookId: card.dataset.bookId,
          title: card.dataset.title,
          author: card.dataset.author,
          cover: card.dataset.cover,
        },
        true,
      ),
    );
    card.remove();
  });

  selectedBooksList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-book]");
    if (!button) return;
    button.closest("[data-selected-book]")?.remove();
  });
}

const qrStart = document.querySelector("#qr-login-start");
const qrStatus = document.querySelector("#qr-login-status");
const qrImage = document.querySelector("#qr-login-image");
let qrTimer = null;

async function pollQrLogin() {
  const response = await fetch("/api/qr-login/status");
  const data = await response.json();
  qrStatus.textContent = data.message;
  if (data.image_ready) {
    qrImage.hidden = false;
    qrImage.src = `/api/qr-login/image?t=${Date.now()}`;
  }
  if (["success", "error", "expired"].includes(data.status)) {
    clearInterval(qrTimer);
    qrStart.disabled = false;
    if (data.status === "success") {
      qrStatus.textContent = "扫码登录成功，登录态已保存";
      window.setTimeout(() => window.location.reload(), 1000);
    }
  }
}

if (qrStart && qrStatus && qrImage) {
  qrStart.addEventListener("click", async () => {
    qrStart.disabled = true;
    qrStatus.textContent = "正在生成二维码...";
    qrImage.hidden = true;
    const response = await fetch("/api/qr-login/start", { method: "POST" });
    const data = await response.json();
    qrStatus.textContent = data.message;
    clearInterval(qrTimer);
    qrTimer = setInterval(pollQrLogin, 2000);
    pollQrLogin();
  });
}

document.querySelectorAll("[data-open-dialog]").forEach((button) => {
  button.addEventListener("click", () => {
    const dialog = document.getElementById(button.dataset.openDialog);
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => {
    button.closest("dialog")?.close();
  });
});
