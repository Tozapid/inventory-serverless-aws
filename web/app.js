(function () {
  const config = window.INVENTORY_CONFIG || {};
  const I18n = window.InventoryI18n;
  const t = I18n.t;
  const root = document.getElementById("root");
  const state = {
    email: "",
    mode: "list",
    query: "",
    items: [],
    cursor: null,
    loading: false,
    selected: null,
    detail: null,
    history: [],
    lightbox: -1,
    editing: false,
    showHidden: false,
    awaitRecognition: null,
    urlsExpireAt: null,
  };
  let pool = null;
  let searchTimer = null;
  let refreshTimer = null;
  let toastTimer = null;

  function copyIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("d", "M8 8.5V6.2A1.2 1.2 0 0 1 9.2 5h8.6A1.2 1.2 0 0 1 19 6.2v8.6a1.2 1.2 0 0 1-1.2 1.2H16M6.2 8H14a1.2 1.2 0 0 1 1.2 1.2v8.6A1.2 1.2 0 0 1 14 19H6.2A1.2 1.2 0 0 1 5 17.8V9.2A1.2 1.2 0 0 1 6.2 8Z");
    svg.append(path);
    return svg;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value == null || value === false) return;
      if (key === "class") node.className = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "value") node.value = value;
      else node.setAttribute(key, value === true ? "" : value);
    });
    [].concat(children || []).forEach((child) => {
      if (child == null || child === false) return;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function toast(message) {
    let node = document.querySelector(".toast");
    if (!node) {
      node = el("div", { class: "toast" });
      document.body.append(node);
    }
    node.textContent = message;
    node.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), 4200);
  }

  function authMessage(error) {
    const code = error && (error.code || error.name);
    if (code === "NotAuthorizedException" || code === "UserNotFoundException") return t("Неверная почта или пароль");
    if (code === "InvalidPasswordException") return t("Пароль должен быть от 8 символов и содержать строчную букву и цифру");
    if (code === "CodeMismatchException" || code === "EnableSoftwareTokenMFAException" || code === "ExpiredCodeException") return t("Неверный код");
    return (error && error.message) || t("Не получилось");
  }

  function userPool() {
    if (!pool) {
      pool = new window.AmazonCognitoIdentity.CognitoUserPool({
        UserPoolId: config.userPoolId,
        ClientId: config.clientId,
      });
    }
    return pool;
  }

  function currentSession() {
    return new Promise((resolve, reject) => {
      const user = userPool().getCurrentUser();
      if (!user) return reject(new Error("auth"));
      user.getSession((error, session) => {
        if (error || !session || !session.isValid()) reject(error || new Error("auth"));
        else resolve(session);
      });
    });
  }

  // A saved session keeps the groups it was issued with for up to an hour.
  // Fresh tokens from the refresh token carry the current groups, so a new
  // administrator gets the user settings without signing out.
  function freshSession() {
    return currentSession().then((session) => new Promise((resolve) => {
      const user = userPool().getCurrentUser();
      if (!user) return resolve(session);
      user.refreshSession(session.getRefreshToken(), (error, fresh) => resolve(error || !fresh ? session : fresh));
    }));
  }

  function applySession(session) {
    const payload = session.getIdToken().payload;
    state.email = payload.email || "";
    state.admin = [].concat(payload["cognito:groups"] || []).includes("admins");
  }

  function sessionExpired() {
    return new Error(t("Сессия закончилась"));
  }

  async function api(path, options) {
    const session = await currentSession();
    const token = session.getIdToken().getJwtToken();
    const response = await fetch(config.apiBase + path, {
      method: (options && options.method) || "GET",
      headers: {
        Authorization: "Bearer " + token,
        "X-Inventory-Lang": I18n.lang,
        ...(options && options.body ? { "Content-Type": "application/json" } : {}),
      },
      body: options && options.body ? JSON.stringify(options.body) : undefined,
    });
    const text = await response.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch (error) { data = { error: text }; }
    }
    if (response.status === 401) {
      signOut();
      throw new Error(t("Сессия закончилась"));
    }
    if (!response.ok) {
      const error = new Error(data.error || t("Запрос не выполнен"));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function imageKind(file) {
    const type = (file.type || "").toLowerCase();
    if (type === "image/jpg" || type === "image/jpeg") return "image/jpeg";
    if (type === "image/png" || type === "image/webp") return type;
    const name = file.name.toLowerCase();
    if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
    if (name.endsWith(".png")) return "image/png";
    if (name.endsWith(".webp")) return "image/webp";
    return null;
  }

  async function encodeJpeg(file, maxEdge, quality) {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    if (bitmap.close) bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (!blob) throw new Error(t("Не удалось подготовить фотографию"));
    return blob;
  }

  async function postFile(post, file) {
    const form = new FormData();
    Object.entries(post.fields).forEach(([key, value]) => form.append(key, value));
    form.append("file", file);
    const response = await fetch(post.url, { method: "POST", body: form });
    if (!response.ok) throw new Error(t("Хранилище не приняло файл"));
  }

  function imageFromClipboard(data) {
    if (!data) return null;
    const files = Array.from(data.files || []);
    const named = files.find((file) => imageKind(file));
    if (named) return named;
    for (const item of Array.from(data.items || [])) {
      if (item.kind !== "file" || !String(item.type || "").startsWith("image/")) continue;
      const file = item.getAsFile();
      if (file && imageKind(file)) return file;
    }
    return null;
  }

  function putPhotoFile(input, file) {
    const kind = imageKind(file);
    if (!kind) throw new Error(t("Нужна фотография JPEG, PNG или WebP"));
    const named = new File([file], file.name || "clipboard.png", { type: kind });
    const transfer = new DataTransfer();
    transfer.items.add(named);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function readClipboardImage() {
    if (!navigator.clipboard || !navigator.clipboard.read) {
      throw new Error(t("Браузер не отдал буфер. Нажмите ⌘V или Ctrl+V"));
    }
    let items = [];
    try {
      items = await navigator.clipboard.read();
    } catch (error) {
      throw new Error(t("Нет доступа к буферу. Нажмите ⌘V или Ctrl+V"));
    }
    for (const item of items) {
      const type = item.types.find((entry) => entry === "image/png" || entry === "image/jpeg" || entry === "image/webp");
      if (!type) continue;
      const blob = await item.getType(type);
      const extension = type === "image/jpeg" ? "jpg" : type === "image/webp" ? "webp" : "png";
      return new File([blob], "clipboard." + extension, { type: type });
    }
    throw new Error(t("В буфере нет изображения"));
  }

  function photoPicker(caption) {
    const input = el("input", { type: "file", accept: "image/jpeg,image/png,image/webp" });
    const preview = el("img", { class: "paste-preview", alt: "" });
    preview.hidden = true;
    const status = el("span", { class: "muted small" }, t("Файл или ⌘V / Ctrl+V"));
    let previewUrl = "";
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      preview.src = previewUrl;
      preview.hidden = false;
      status.textContent = file.name && !file.name.startsWith("clipboard.") ? file.name : t("Изображение из буфера");
    });
    const node = el("div", { class: "photo-picker" }, [
      el("span", { class: "small" }, caption),
      el("div", { class: "paste-row" }, [
        input,
        el("button", {
          class: "ghost",
          type: "button",
          onclick: async () => {
            try {
              putPhotoFile(input, await readClipboardImage());
            } catch (error) {
              status.textContent = error.message;
            }
          },
        }, t("Из буфера")),
        preview,
      ]),
      status,
    ]);
    return { input: input, node: node };
  }

  function photoInputForPaste() {
    const active = document.activeElement;
    const form = active && active.closest && active.closest("form.composer");
    if (form) return form.querySelector("input[type=file]");
    return document.querySelector(".composer input[type=file]");
  }

  let photoBusy = false;

  async function saveCardPhoto(file) {
    const number = state.selected;
    if (!number || photoBusy) return;
    const status = document.querySelector("#detail .photo-status");
    photoBusy = true;
    if (status) status.textContent = t("Сохраняю фотографию…");
    try {
      const saved = await uploadPhoto(number, file);
      if (state.selected !== number) return;
      if (state.detail && state.detail.number === saved.number) {
        state.detail.photo_url = saved.photo_url;
        state.detail.thumb_url = saved.thumb_url;
        state.detail.has_photo = true;
        state.detail.version = saved.version;
        state.detail.updated_at = saved.updated_at;
        state.detail.updated_by = saved.updated_by;
      }
      const frame = document.querySelector("#detail .frame");
      if (frame && saved.photo_url) {
        frame.replaceChildren(el("button", {
          type: "button",
          onclick: () => openOriginal(state.detail),
        }, [
          el("img", { src: saved.photo_url, alt: t("Коробка {number}", { number: saved.number }) }),
        ]));
      }
      const savedMessage = saved.recognition_pending
        ? t("Фотография сохранена, распознавание запущено")
        : t("Фотография сохранена");
      if (status) status.textContent = savedMessage;
      toast(savedMessage);
      if (saved.recognition_pending) {
        state.awaitRecognition = number;
        state.detail.hidden_text = "";
        watchRecognition(number);
      }
      await loadBoxes(false);
    } catch (error) {
      if (status) status.textContent = error.message;
      toast(error.message);
    } finally {
      photoBusy = false;
    }
  }

  async function uploadPhoto(number, file) {
    const kind = imageKind(file);
    if (!kind) throw new Error(t("Нужна фотография JPEG, PNG или WebP"));
    if (file.size > 40 * 1024 * 1024) throw new Error(t("Файл больше 40 МБ"));
    const view = await encodeJpeg(file, 2560, 0.82);
    const thumb = await encodeJpeg(file, 1280, 0.8);
    const signed = await api("/boxes/" + encodeURIComponent(number) + "/photo", {
      method: "POST",
      body: { content_type: "image/jpeg" },
    });
    await postFile(signed.original, new File([view], "photo.jpg", { type: "image/jpeg" }));
    await postFile(signed.thumb, new File([thumb], "thumb.jpg", { type: "image/jpeg" }));
    return api("/boxes/" + encodeURIComponent(number) + "/photo/complete", {
      method: "POST",
      body: { original_key: signed.original.key, thumb_key: signed.thumb.key },
    });
  }

  function languagePicker() {
    return el("select", {
      class: "lang-select",
      "aria-label": t("Язык"),
      onchange: (event) => switchLanguage(event.target.value),
    }, I18n.languages.map(([code, name]) => el("option", { value: code, selected: code === I18n.lang ? "selected" : null }, name)));
  }

  // The page is built in the chosen language, so switching reloads it.
  function switchLanguage(code) {
    if (code === I18n.lang) return;
    I18n.setLang(code);
    window.location.reload();
  }

  function showLogin(message) {
    const error = el("p", { class: "error" }, message || "");
    const demo = config.demo || {};
    const email = el("input", { type: "email", autocomplete: "username", required: "required", value: demo.email || "" });
    const password = el("input", { type: "password", autocomplete: "current-password", required: "required", value: demo.password || "" });
    const form = el("form", {
      onsubmit: (event) => {
        event.preventDefault();
        signIn(email.value.trim(), password.value, error);
      },
    }, [
      el("label", {}, [t("Почта"), email]),
      el("label", {}, [t("Пароль"), password]),
      el("button", { class: "primary", type: "submit" }, t("Войти")),
      error,
    ]);
    root.replaceChildren(el("main", { class: "login" }, [
      el("section", { class: "login-card" }, [
        el("div", { class: "mark" }, t("К")),
        el("h1", {}, t("Инвентарь коробок")),
        el("p", { class: "lede" }, t("Вход только для приглашённых пользователей.")),
        demo.email ? el("p", { class: "demo-note" }, [
          t("Демо: {email} / {password}.", { email: demo.email, password: demo.password }),
          demo.reset ? " " + t("Все данные и пользователи стираются каждый час.") : "",
        ]) : null,
        form,
        el("div", { class: "login-lang" }, languagePicker()),
      ]),
    ]));
  }

  function showNewPassword(user, attributes, message) {
    const error = el("p", { class: "error" }, message || "");
    const password = el("input", { type: "password", autocomplete: "new-password", required: "required", minlength: "8" });
    root.replaceChildren(el("main", { class: "login" }, [
      el("section", { class: "login-card" }, [
        el("h1", {}, t("Новый пароль")),
        el("p", { class: "lede" }, t("Временный пароль нужно сменить перед входом.")),
        el("form", {
          onsubmit: (event) => {
            event.preventDefault();
            user.completeNewPasswordChallenge(password.value, {}, {
              onSuccess: () => boot(),
              onFailure: (err) => { error.textContent = authMessage(err); },
              totpRequired: () => showTotp(user),
            });
          },
        }, [
          el("label", {}, [t("Новый пароль"), password]),
          el("button", { class: "primary", type: "submit" }, t("Сохранить и войти")),
          error,
        ]),
      ]),
    ]));
  }

  // A user with an authenticator app is asked for its code after the password.
  // Cognito gives three minutes for it; after that the password is asked again.
  function showTotp(user) {
    const error = el("p", { class: "error", role: "alert" });
    const code = el("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: "6", required: true });
    root.replaceChildren(el("main", { class: "login" }, [
      el("section", { class: "login-card" }, [
        el("h1", {}, t("Код из приложения")),
        el("p", { class: "lede" }, t("Введите шестизначный код из приложения-аутентификатора.")),
        el("form", {
          onsubmit: (event) => {
            event.preventDefault();
            user.sendMFACode(code.value.trim(), {
              onSuccess: () => boot(),
              onFailure: (err) => {
                if ((err && (err.code || err.name)) === "NotAuthorizedException") {
                  showLogin(t("Время на ввод кода вышло. Войдите ещё раз."));
                  return;
                }
                error.textContent = authMessage(err);
                code.value = "";
                code.focus();
              },
            }, "SOFTWARE_TOKEN_MFA");
          },
        }, [
          el("label", {}, [t("Код"), code]),
          el("button", { class: "primary", type: "submit" }, t("Войти")),
          error,
        ]),
        el("button", { type: "button", class: "link-button", onclick: () => showLogin() }, t("Назад")),
      ]),
    ]));
    code.focus();
  }

  function signIn(email, password, errorNode) {
    const user = new window.AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool() });
    user.authenticateUser(new window.AmazonCognitoIdentity.AuthenticationDetails({
      Username: email,
      Password: password,
    }), {
      onSuccess: () => boot(),
      onFailure: (error) => { errorNode.textContent = authMessage(error); },
      newPasswordRequired: (attributes) => showNewPassword(user, attributes),
      totpRequired: () => showTotp(user),
    });
  }

  function signOut() {
    const user = pool && pool.getCurrentUser();
    if (user) user.signOut();
    clearTimeout(refreshTimer);
    showLogin();
  }

  function boxPath(number) {
    return "/box/" + encodeURIComponent(number);
  }

  function numberFromLocation() {
    let path = location.pathname || "/";
    try { path = decodeURIComponent(path); } catch (error) { return null; }
    if (path.length > 1) path = path.replace(/\/+$/, "");
    const match = path.match(/^\/box\/([^/]+)$/);
    return match ? match[1] : null;
  }

  function plainClick(event) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
  }

  function setRoute(number, mode) {
    const path = number ? boxPath(number) : "/";
    document.title = number ? t("Коробка {number} — Инвентарь", { number }) : t("Инвентарь коробок");
    if (location.pathname === path || mode === "none") return;
    if (mode === "replace") history.replaceState(null, "", path);
    else history.pushState(null, "", path);
  }

  function followLink(event, action) {
    if (!plainClick(event)) return;
    event.preventDefault();
    action();
  }

  function renderApp() {
    const search = el("input", {
      type: "search",
      placeholder: t("Поиск по номеру, описанию и содержимому"),
      value: state.query,
      oninput: (event) => {
        state.query = event.target.value;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => loadBoxes(false), 250);
      },
    });
    root.replaceChildren(el("div", { class: "app" }, [
      el("header", { class: "top" }, [
        el("a", {
          class: "brand",
          href: "/",
          onclick: (event) => followLink(event, closeDetail),
        }, [
          el("div", { class: "mark" }, t("К")),
          el("div", {}, [el("strong", {}, t("Коробки")), el("span", {}, t("инвентарь"))]),
        ]),
        el("div", { class: "search" }, search),
        el("div", { class: "seg" }, [
          el("button", { type: "button", class: state.mode === "list" ? "active" : "", onclick: () => setMode("list") }, t("Список")),
          el("button", { type: "button", class: state.mode === "photos" ? "active" : "", onclick: () => setMode("photos") }, t("Фото")),
        ]),
        el("div", { class: "who" }, [
          el("span", {}, state.email),
          el("button", { class: "icon-button", type: "button", onclick: () => showSettings() }, t("Настройки")),
          el("button", { class: "icon-button", type: "button", onclick: signOut }, t("Выйти")),
        ]),
      ]),
      el("div", { class: "workspace" }, [
        el("section", { class: "catalog", id: "catalog" }),
        el("aside", { class: "detail is-empty", id: "detail" }),
      ]),
    ]));
    renderCatalog();
    renderDetail();
  }

  function setMode(mode) {
    state.mode = mode;
    renderApp();
  }

  function renderCatalog() {
    const catalog = document.getElementById("catalog");
    if (!catalog) return;
    if (!document.getElementById("catalog-head")) {
      catalog.replaceChildren(
        el("div", { class: "toolbar", id: "catalog-head" }),
        el("div", { id: "composer-slot" }),
        el("div", { id: "catalog-body" })
      );
    }
    document.getElementById("catalog-head").replaceChildren(
      el("h2", {}, state.query ? t("Найдено") : t("Все коробки")),
      el("span", { class: "muted small" }, state.items.length ? String(state.items.length) : ""),
      el("button", { class: "primary", type: "button", onclick: toggleComposer }, composerOpen ? t("Скрыть форму") : t("Новая коробка"))
    );
    const slot = document.getElementById("composer-slot");
    if (!composerOpen) slot.replaceChildren();
    else if (!slot.querySelector("form")) slot.replaceChildren(composerNode());
    const body = state.loading
      ? el("p", { class: "muted" }, t("Загрузка…"))
      : state.items.length
        ? (state.mode === "photos" ? renderGallery() : renderList())
        : el("div", { class: "empty" }, [
          el("h3", {}, state.query ? t("Ничего не нашлось") : t("Пока пусто")),
          el("p", { class: "lede" }, state.query ? t("Попробуйте другое слово или номер.") : t("Добавьте первую коробку: номер, описание и фотографию.")),
        ]);
    const holder = document.getElementById("catalog-body");
    holder.replaceChildren(body);
    if (state.cursor) holder.append(el("button", { class: "ghost", type: "button", onclick: () => loadBoxes(true) }, t("Показать ещё")));
  }

  let composerOpen = false;

  function toggleComposer() {
    composerOpen = !composerOpen;
    renderCatalog();
  }

  function composerNode() {
    if (!composerOpen) return null;
    const number = el("input", { required: "required", maxlength: "41", autocomplete: "off" });
    const description = el("textarea", { maxlength: "4000" });
    const contents = el("textarea", { maxlength: "20000" });
    const photo = photoPicker(t("Фотография"));
    const error = el("p", { class: "error" });
    return el("form", {
      class: "composer",
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        const button = event.target.querySelector("button[type=submit]");
        button.disabled = true;
        try {
          const created = await api("/boxes", {
            method: "POST",
            body: {
              number: number.value.trim(),
              description: description.value,
              contents: contents.value,
            },
          });
          if (photo.input.files[0]) {
            button.textContent = t("Загрузка фотографии…");
            await uploadPhoto(created.number, photo.input.files[0]);
          }
          composerOpen = false;
          state.query = "";
          await loadBoxes(false);
          await openBox(created.number);
        } catch (err) {
          error.textContent = err.message;
          button.disabled = false;
          button.textContent = t("Создать");
        }
      },
    }, [
      el("h3", {}, t("Новая коробка")),
      el("label", {}, [t("Номер"), number]),
      markdownField(t("Описание"), description, el("div", { class: "md" })),
      markdownField(t("Содержимое"), contents, el("div", { class: "md" })),
      photo.node,
      el("div", { class: "row-actions" }, [
        el("button", { class: "primary", type: "submit" }, t("Создать")),
        el("button", { class: "ghost", type: "button", onclick: toggleComposer }, t("Отмена")),
      ]),
      error,
    ]);
  }

  function renderList() {
    return el("div", { class: "list" }, state.items.map((item) => el("a", {
      class: "box-row" + (item.number === state.selected ? " selected" : ""),
      href: boxPath(item.number),
      onclick: (event) => followLink(event, () => openBox(item.number)),
    }, [
      photoSlot(item.thumb_url, item.number, "thumb", true),
      el("span", { class: "box-copy" }, listText(item)),
    ])));
  }

  function renderGallery() {
    return el("div", { class: "gallery" }, state.items.map((item, index) => el("div", { class: "tile" }, [
      el("button", {
        class: "photo",
        type: "button",
        onclick: () => (item.photo_url || item.thumb_url ? openLightbox(index) : openBox(item.number)),
      }, [photoSlot(item.thumb_url || item.photo_url, item.number, "frame", true)]),
      el("a", {
        class: "tile-link",
        href: boxPath(item.number),
        onclick: (event) => followLink(event, () => openBox(item.number)),
      }, [el("span", { class: "plate" }, item.number), " ", plainTitle(item.description) || t("Без описания")]),
    ])));
  }

  function photoSlot(url, number, className, lazy) {
    if (!url) return el("span", { class: className }, t("нет фото"));
    const image = { src: url, alt: t("Коробка {number}", { number }) };
    if (lazy) image.loading = "lazy";
    return el("span", { class: className }, [
      el("img", image),
    ]);
  }

  async function loadBoxes(append) {
    state.loading = !append && state.items.length === 0;
    renderCatalog();
    try {
      const params = new URLSearchParams();
      if (state.query.trim()) params.set("q", state.query.trim());
      if (append && state.cursor) params.set("cursor", state.cursor);
      const data = await api("/boxes?" + params.toString());
      state.items = append ? state.items.concat(data.items) : data.items;
      state.cursor = data.next_cursor;
      state.urlsExpireAt = data.urls_expire_at;
      scheduleRefresh();
    } catch (error) {
      toast(error.message);
    } finally {
      state.loading = false;
      renderCatalog();
      if (state.detail) refreshDetailPhoto();
    }
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    const when = state.urlsExpireAt ? new Date(state.urlsExpireAt).getTime() - Date.now() - 60000 : 12 * 60 * 1000;
    refreshTimer = setTimeout(() => loadBoxes(false), Math.max(when, 20000));
  }

  function listText(item) {
    const parts = [el("span", { class: "plate" }, item.number)];
    if (String(item.description || "").trim()) parts.push(markdownNode(item.description, true));
    else parts.push(el("span", { class: "muted small" }, t("Без описания")));
    return parts;
  }

  async function openBox(number, mode) {
    if (state.selected !== number) {
      state.editing = false;
      state.showHidden = false;
    }
    state.selected = number;
    setRoute(number, mode || "push");
    renderCatalog();
    const detail = document.getElementById("detail");
    if (detail) {
      detail.classList.remove("is-empty");
      detail.replaceChildren(el("p", { class: "muted" }, t("Открываю карточку…")));
    }
    try {
      const [card, history] = await Promise.all([
        api("/boxes/" + encodeURIComponent(number)),
        api("/boxes/" + encodeURIComponent(number) + "/history"),
      ]);
      if (state.selected !== number) return;
      state.detail = card;
      state.history = history.items || [];
      renderDetail();
    } catch (error) {
      if (state.selected !== number) return;
      state.detail = null;
      state.history = [];
      showMissingCard(number, error.message);
    }
  }

  function showMissingCard(number, message) {
    const detail = document.getElementById("detail");
    if (!detail) return;
    detail.classList.remove("is-empty");
    detail.replaceChildren(
      el("button", { class: "ghost back", type: "button", onclick: closeDetail }, t("К списку")),
      el("h2", {}, number),
      el("p", { class: "lede" }, message)
    );
  }

  function closeDetail() {
    state.selected = null;
    state.detail = null;
    state.history = [];
    state.editing = false;
    state.showHidden = false;
    state.awaitRecognition = null;
    clearTimeout(recognitionTimer);
    setRoute(null, "push");
    renderCatalog();
    renderDetail();
  }

  let recognitionTimer = null;

  function watchRecognition(number) {
    clearTimeout(recognitionTimer);
    let tries = 0;
    const tick = async () => {
      if (state.selected !== number || state.awaitRecognition !== number) return;
      tries += 1;
      try {
        const fresh = await api("/boxes/" + encodeURIComponent(number));
        if (state.selected !== number) return;
        if (String(fresh.hidden_text || "").trim()) {
          state.awaitRecognition = null;
          state.detail = fresh;
          renderDetail();
          return;
        }
      } catch (error) {
        return;
      }
      if (tries < 12) recognitionTimer = setTimeout(tick, 5000);
      else state.awaitRecognition = null;
    };
    recognitionTimer = setTimeout(tick, 5000);
  }

  function recognitionControl(card) {
    const hidden = String(card.hidden_text || "").trim();
    if (!hidden) {
      if (state.awaitRecognition === card.number) return el("p", { class: "muted small" }, t("Распознавание…"));
      return null;
    }
    return el("div", { class: "hidden-text" }, [
      el("button", {
        class: "ghost tiny",
        type: "button",
        onclick: () => {
          state.showHidden = !state.showHidden;
          renderDetail();
        },
      }, state.showHidden ? t("Скрыть") : t("Распознавание")),
      state.showHidden ? markdownNode(hidden) : null,
    ].filter(Boolean));
  }

  function renderDetail() {
    const node = document.getElementById("detail");
    if (!node) return;
    const card = state.detail;
    if (!card || card.number !== state.selected) {
      node.classList.add("is-empty");
      node.replaceChildren(
        el("h2", {}, t("Карточка")),
        el("p", { class: "lede" }, t("Выберите коробку в списке или откройте фотографию."))
      );
      return;
    }
    node.classList.remove("is-empty");
    const fileInput = el("input", { type: "file", accept: "image/jpeg,image/png,image/webp" });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (file) saveCardPhoto(file);
    });
    const image = card.photo_url
      ? el("button", { type: "button", onclick: () => openOriginal(card) }, [
        el("img", { src: card.photo_url, alt: t("Коробка {number}", { number: card.number }) }),
      ])
      : el("span", { class: "muted" }, t("Фотографии пока нет"));
    const chrome = [
      el("div", { class: "detail-tools" }, [
        state.editing ? null : el("button", {
          class: "primary",
          type: "button",
          onclick: () => {
            state.editing = true;
            renderDetail();
          },
        }, t("Редактировать")),
        el("button", {
          class: "ghost",
          type: "button",
          title: t("Закрыть карточку и не записывать текст"),
          onclick: closeDetail,
        }, t("Закрыть")),
      ]),
      el("div", { class: "detail-head" }, [
        el("span", { class: "plate" }, card.number),
        el("div", {}, [
          el("h2", {}, plainTitle(card.description) || t("Без описания")),
          el("div", { class: "link-line" }, [
            el("a", {
              class: "card-link",
              href: boxPath(card.number),
              onclick: (event) => followLink(event, () => {}),
            }, boxPath(card.number)),
            el("button", {
              class: "copy",
              type: "button",
              title: t("Скопировать ссылку"),
              "aria-label": t("Скопировать ссылку"),
              onclick: async () => {
                const url = location.origin + boxPath(card.number);
                try {
                  await navigator.clipboard.writeText(url);
                  toast(t("Ссылка скопирована"));
                } catch (copyError) {
                  toast(url);
                }
              },
            }, [copyIcon()]),
          ]),
          el("p", { class: "muted small" }, t("Обновил {who} · {when}", { who: card.updated_by || "—", when: formatWhen(card.updated_at) })),
        ]),
      ]),
      el("div", { class: "frame" }, image),
    ];
    const photoActions = el("div", { class: "photo-actions" }, [
      el("button", {
        class: "primary",
        type: "button",
        onclick: async () => {
          try {
            await saveCardPhoto(await readClipboardImage());
          } catch (pasteError) {
            const status = document.querySelector("#detail .photo-status");
            if (status) status.textContent = pasteError.message;
          }
        },
      }, t("Вставить и сохранить")),
      el("label", { class: "ghost file-label" }, [t("Файл"), fileInput]),
      el("span", { class: "photo-status muted small" }, t("⌘V тоже сохраняет фотографию сразу")),
    ]);
    if (!state.editing) {
      const description = String(card.description || "");
      const formattedDescription = description.trim() && description.trim() !== plainTitle(description)
        ? markdownNode(description)
        : null;
      node.replaceChildren(...chrome.concat([
        formattedDescription,
        markdownNode(card.contents || ""),
        recognitionControl(card),
        el("div", { class: "history-head" }, [el("h3", {}, t("История содержимого"))]),
        renderHistory(),
      ]).filter(Boolean));
      return;
    }
    const description = el("textarea", { maxlength: "4000" }, card.description || "");
    const contents = el("textarea", { maxlength: "20000" }, card.contents || "");
    const note = el("input", { maxlength: "500", placeholder: t("Зачем поменяли содержимое") });
    const error = el("p", { class: "error" });
    node.replaceChildren(
      ...chrome,
      photoActions,
      el("form", {
        class: "stack",
        onsubmit: async (event) => {
          event.preventDefault();
          error.textContent = "";
          try {
            const saved = await api("/boxes/" + encodeURIComponent(card.number), {
              method: "PATCH",
              body: {
                description: description.value,
                contents: contents.value,
                note: note.value,
                version: card.version,
              },
            });
            state.editing = false;
            state.detail = saved;
            await openBox(card.number);
            await loadBoxes(false);
            toast(t("Карточка сохранена"));
          } catch (err) {
            error.textContent = err.message;
            if (err.status === 409) openBox(card.number);
          }
        },
      }, [
        el("label", {}, [t("Описание"), description]),
        el("label", {}, [t("Содержимое, можно с markdown"), contents]),
        el("label", {}, [t("Комментарий к изменению содержимого"), note]),
        el("div", { class: "row-actions" }, [
          el("button", { class: "primary", type: "submit" }, t("Сохранить")),
          el("button", {
            class: "ghost",
            type: "button",
            onclick: () => {
              state.editing = false;
              renderDetail();
            },
          }, t("Отмена")),
          el("button", {
            class: "danger",
            type: "button",
            onclick: async () => {
              if (!window.confirm(t("Удалить коробку {number}?", { number: card.number }))) return;
              try {
                await api("/boxes/" + encodeURIComponent(card.number), { method: "DELETE" });
                closeDetail();
                await loadBoxes(false);
              } catch (err) {
                error.textContent = err.message;
              }
            },
          }, t("Удалить")),
        ]),
        error,
      ]),
      el("div", { class: "history-head" }, [el("h3", {}, t("История содержимого"))]),
      renderHistory()
    );
  }

  function refreshDetailPhoto() {
    const card = state.items.find((item) => item.number === state.selected);
    const image = document.querySelector("#detail .frame img");
    if (card && image && card.photo_url) image.src = card.photo_url;
  }

  function safeUrl(url) {
    if (/^https?:\/\//i.test(url)) return url;
    if (/^mailto:/i.test(url)) return url;
    return "";
  }

  function appendBreaks(parent, text) {
    String(text).split("\n").forEach((part, index) => {
      if (index) parent.append(document.createElement("br"));
      if (part) parent.append(document.createTextNode(part));
    });
  }

  function appendInline(parent, text, compact) {
    const pattern = /(`[^`\n]+`)|(\[[^\]\n]+\]\([^)\s]+\))|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\*[^*\n]+\*|_[^_\n]+_)/g;
    const source = String(text);
    let last = 0;
    let match;
    while ((match = pattern.exec(source))) {
      if (match.index > last) appendBreaks(parent, source.slice(last, match.index));
      const token = match[0];
      if (token.startsWith("`")) {
        parent.append(el("code", {}, token.slice(1, -1)));
      } else if (token.startsWith("[")) {
        const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
        const href = link && safeUrl(link[2]);
        if (href && !compact) {
          parent.append(el("a", { href: href, target: "_blank", rel: "noopener noreferrer" }, link[1]));
        } else if (link) {
          appendInline(parent, link[1], compact);
        } else {
          appendBreaks(parent, token);
        }
      } else if (token.startsWith("**") || token.startsWith("__")) {
        const strong = el("strong");
        appendInline(strong, token.slice(2, -2), compact);
        parent.append(strong);
      } else {
        const em = el("em");
        appendInline(em, token.slice(1, -1), compact);
        parent.append(em);
      }
      last = match.index + token.length;
    }
    if (last < source.length) appendBreaks(parent, source.slice(last));
  }

  function renderMarkdown(source, compact) {
    const root = document.createDocumentFragment();
    const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
    let index = 0;
    let paragraph = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      const node = el("p");
      appendInline(node, paragraph.join("\n"), compact);
      root.append(node);
      paragraph = [];
    }

    while (index < lines.length) {
      const line = lines[index];
      if (/^```/.test(line)) {
        flushParagraph();
        const code = [];
        index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        root.append(el("pre", {}, [el("code", {}, code.join("\n"))]));
        index += 1;
        continue;
      }
      const heading = /^(#{1,3})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph();
        const node = el("h" + (heading[1].length + 2));
        appendInline(node, heading[2], compact);
        root.append(node);
        index += 1;
        continue;
      }
      if (/^>\s?/.test(line)) {
        flushParagraph();
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^>\s?/, ""));
          index += 1;
        }
        const node = el("blockquote");
        appendInline(node, quote.join("\n"), compact);
        root.append(node);
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        flushParagraph();
        const list = el("ul");
        while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
          const item = el("li");
          appendInline(item, lines[index].replace(/^\s*[-*]\s+/, ""), compact);
          list.append(item);
          index += 1;
        }
        root.append(list);
        continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        flushParagraph();
        const list = el("ol");
        while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
          const item = el("li");
          appendInline(item, lines[index].replace(/^\s*\d+\.\s+/, ""), compact);
          list.append(item);
          index += 1;
        }
        root.append(list);
        continue;
      }
      if (line.trim() === "") {
        flushParagraph();
        index += 1;
        continue;
      }
      paragraph.push(line);
      index += 1;
    }
    flushParagraph();
    return root;
  }

  function markdownNode(text, compact) {
    const node = el("div", { class: compact ? "md md-compact" : "md" });
    fillMarkdown(node, text, compact);
    return node;
  }

  function fillMarkdown(node, text, compact) {
    if (!String(text || "").trim()) {
      node.replaceChildren(el("p", { class: "muted" }, t("Ничего не написано")));
      return;
    }
    node.replaceChildren(renderMarkdown(text, compact));
  }

  function plainTitle(text) {
    const line = String(text || "").split("\n").map((part) => part.trim()).find(Boolean) || "";
    return line
      .replace(/^#{1,6}\s+/, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/[*_`]/g, "")
      .trim();
  }

  function markdownField(label, field, preview) {
    field.addEventListener("input", () => fillMarkdown(preview, field.value));
    fillMarkdown(preview, field.value);
    return el("div", { class: "md-field" }, [
      el("label", {}, [label, field]),
      preview,
    ]);
  }

  // The API writes this note itself when a box is created; people's own notes
  // stay as they were typed.
  function historyNote(note) {
    return note === "Создание коробки" ? t("Создание коробки") : note;
  }

  function renderHistory() {
    if (!state.history.length) return el("p", { class: "muted small" }, t("Изменений содержимого ещё не было."));
    return el("div", { class: "history" }, state.history.map((item) => el("article", { class: "event" }, [
      el("strong", {}, formatWhen(item.at)),
      el("span", { class: "muted small" }, [item.by || "—", item.note ? " · " + historyNote(item.note) : ""]),
      el("div", { class: "diff" }, [
        el("div", {}, [el("span", { class: "muted small" }, t("Было")), markdownNode(item.before)]),
        el("div", {}, [el("span", { class: "muted small" }, t("Стало")), markdownNode(item.after)]),
      ]),
    ])));
  }

  function photoItems() {
    return state.items.filter((item) => item.photo_url || item.thumb_url);
  }

  function openLightbox(index) {
    const item = state.items[index];
    if (!item) return;
    const photos = photoItems();
    const photoIndex = photos.findIndex((candidate) => candidate.number === item.number);
    state.lightbox = photoIndex === -1 ? 0 : photoIndex;
    renderLightbox();
  }

  function openOriginal(card) {
    const photos = photoItems();
    let photoIndex = photos.findIndex((item) => item.number === card.number);
    if (photoIndex === -1) {
      photos.unshift(card);
      photoIndex = 0;
    }
    state.lightbox = photoIndex;
    renderLightbox();
  }

  function renderLightbox() {
    document.querySelector(".lightbox")?.remove();
    const photos = photoItems();
    const item = photos[state.lightbox];
    if (!item) return;
    const overlay = el("div", { class: "lightbox" }, [
      el("header", {}, [
        el("strong", {}, item.number + (plainTitle(item.description) ? " — " + plainTitle(item.description) : "")),
        el("button", { class: "icon-button", type: "button", onclick: closeLightbox }, t("Закрыть")),
      ]),
      el("img", { src: item.photo_url || item.thumb_url, alt: t("Коробка {number}", { number: item.number }) }),
      el("footer", {}, [
        el("button", { class: "icon-button", type: "button", onclick: () => stepLightbox(-1) }, t("Предыдущая")),
        el("button", { class: "ghost", type: "button", onclick: () => openBox(item.number).then(closeLightbox) }, t("Открыть карточку")),
        el("button", { class: "icon-button", type: "button", onclick: () => stepLightbox(1) }, t("Следующая")),
      ]),
    ]);
    document.body.append(overlay);
  }

  function stepLightbox(delta) {
    const photos = photoItems();
    if (!photos.length) return;
    state.lightbox = (state.lightbox + delta + photos.length) % photos.length;
    renderLightbox();
  }

  function closeLightbox() {
    state.lightbox = -1;
    document.querySelector(".lightbox")?.remove();
  }

  function changePassword(oldPassword, newPassword) {
    return new Promise((resolve, reject) => {
      const user = userPool().getCurrentUser();
      if (!user) return reject(sessionExpired());
      user.getSession((sessionError) => {
        if (sessionError) return reject(sessionError);
        user.changePassword(oldPassword, newPassword, (changeError) => {
          if (changeError) reject(changeError);
          else resolve();
        });
      });
    });
  }

  // Authenticator app (TOTP). Cognito keeps the secret; the page shows it
  // once as a QR code while the app is being connected.

  function signedInUser() {
    return new Promise((resolve, reject) => {
      const user = userPool().getCurrentUser();
      if (!user) return reject(sessionExpired());
      user.getSession((error) => (error ? reject(error) : resolve(user)));
    });
  }

  function totpEnabled(user) {
    return new Promise((resolve, reject) => {
      user.getUserData((error, data) => {
        if (error) reject(error);
        else resolve((data.UserMFASettingList || []).includes("SOFTWARE_TOKEN_MFA"));
      }, { bypassCache: true });
    });
  }

  function newTotpSecret(user) {
    return new Promise((resolve, reject) => {
      user.associateSoftwareToken({ associateSecretCode: resolve, onFailure: reject });
    });
  }

  function verifyTotp(user, code) {
    return new Promise((resolve, reject) => {
      user.verifySoftwareToken(code, t("Инвентарь коробок"), { onSuccess: resolve, onFailure: reject });
    });
  }

  function setTotp(user, enabled) {
    return new Promise((resolve, reject) => {
      user.setUserMfaPreference(null, { Enabled: enabled, PreferredMfa: enabled }, (error) => (error ? reject(error) : resolve()));
    });
  }

  // The QR code is drawn as SVG from the qrcode-generator matrix, dark on
  // white in both themes so that phone cameras read it.
  function qrCode(text) {
    if (!window.qrcode) return null;
    const qr = window.qrcode(0, "M");
    qr.addData(text);
    qr.make();
    const count = qr.getModuleCount();
    const size = count + 8;
    let d = "";
    for (let row = 0; row < count; row += 1) {
      for (let col = 0; col < count; col += 1) {
        if (qr.isDark(row, col)) d += "M" + (col + 4) + " " + (row + 4) + "h1v1h-1z";
      }
    }
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + size + " " + size);
    svg.setAttribute("class", "qr");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", t("QR-код для приложения-аутентификатора"));
    const back = document.createElementNS(ns, "rect");
    back.setAttribute("width", size);
    back.setAttribute("height", size);
    back.setAttribute("fill", "#fff");
    const dots = document.createElementNS(ns, "path");
    dots.setAttribute("d", d);
    dots.setAttribute("fill", "#000");
    dots.setAttribute("shape-rendering", "crispEdges");
    svg.append(back, dots);
    return svg;
  }

  // Settings -----------------------------------------------------------
  // Everyone changes their password, two-step sign-in, photo recognition and
  // language here. Administrators also manage users.

  const USER_STATUS = {
    CONFIRMED: t("Активен"),
    FORCE_CHANGE_PASSWORD: t("Ждёт первого входа"),
    RESET_REQUIRED: t("Нужен новый пароль"),
    UNCONFIRMED: t("Не подтверждён"),
  };

  async function showSettings(section) {
    try {
      applySession(await freshSession());
    } catch (error) {
      // Offline or signed out: show what the current session allows.
    }
    const sections = [["password", t("Пароль")], ["totp", t("Вход в два шага")]];
    if (config.recognition) sections.push(["recognition", t("Распознавание")]);
    sections.push(["language", t("Язык")]);
    if (state.admin) sections.push(["users", t("Пользователи")]);
    let current = sections.some(([key]) => key === section) ? section : "password";
    const body = el("div", { class: "settings-body" });
    const tabs = el("div", { class: "tabs", role: "tablist" });
    const wrap = el("div", { class: "dialog-wrap", onclick: (event) => { if (event.target === wrap) wrap.remove(); } }, [
      el("section", { class: "dialog wide", role: "dialog", "aria-modal": "true", "aria-label": t("Настройки") }, [
        el("header", { class: "dialog-head" }, [
          el("h2", {}, t("Настройки")),
          el("button", { class: "icon-button", type: "button", onclick: () => wrap.remove() }, t("Закрыть")),
        ]),
        tabs,
        body,
      ]),
    ]);
    function draw() {
      tabs.replaceChildren(...sections.map(([key, label]) => el("button", {
        type: "button",
        role: "tab",
        class: key === current ? "tab active" : "tab",
        "aria-selected": String(key === current),
        onclick: () => {
          current = key;
          draw();
        },
      }, label)));
      body.replaceChildren();
      if (current === "language") languageSection(body);
      else if (current === "users") usersSection(body);
      else if (current === "recognition") recognitionSection(body, wrap);
      else if (current === "totp") totpSection(body);
      else passwordSection(body, wrap);
    }
    document.querySelector(".dialog-wrap")?.remove();
    document.body.append(wrap);
    draw();
  }

  function languageSection(body) {
    body.append(el("fieldset", { class: "languages" }, [
      el("legend", {}, t("Язык интерфейса")),
      ...I18n.languages.map(([code, name]) => el("label", { class: "check" }, [
        el("input", { type: "radio", name: "lang", value: code, checked: code === I18n.lang, onchange: () => switchLanguage(code) }),
        name,
      ])),
      el("p", { class: "hint" }, I18n.chosen()
        ? t("Выбор хранится в этом браузере.")
        : t("Сейчас язык взят из настроек системы.")),
    ]));
  }

  function passwordSection(body, wrap) {
    const error = el("p", { class: "error", role: "alert" });
    const oldPassword = el("input", { type: "password", autocomplete: "current-password", required: true });
    const newPassword = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: "8" });
    body.append(el("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        try {
          await changePassword(oldPassword.value, newPassword.value);
          wrap.remove();
          toast(t("Пароль изменён"));
        } catch (err) {
          error.textContent = authMessage(err);
        }
      },
    }, [
      el("label", {}, [t("Текущий пароль"), oldPassword]),
      el("label", {}, [t("Новый пароль"), newPassword]),
      el("p", { class: "hint" }, t("Не короче 8 символов, со строчной буквой и цифрой.")),
      el("div", { class: "dialog-actions" }, [el("button", { type: "submit", class: "primary" }, t("Сменить пароль"))]),
      error,
    ]));
    oldPassword.focus();
  }

  // Photo recognition: each user keeps their own Grok key and model.
  async function recognitionSection(body, wrap) {
    let settings = { grok_api_key: "", grok_model: "grok-4.7" };
    try {
      settings = Object.assign(settings, await api("/settings"));
    } catch (loadError) {
      toast(loadError.message);
    }
    const apiKey = el("input", { type: "password", autocomplete: "off", value: settings.grok_api_key || "" });
    const model = el("input", { type: "text", autocomplete: "off", value: settings.grok_model || "grok-4.7", placeholder: "grok-4.7" });
    const error = el("p", { class: "error", role: "alert" });
    body.append(el("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        try {
          await api("/settings", { method: "PUT", body: { grok_api_key: apiKey.value.trim(), grok_model: model.value.trim() || "grok-4.7" } });
          wrap.remove();
          toast(t("Настройки сохранены"));
        } catch (saveError) {
          error.textContent = saveError.message;
        }
      },
    }, [
      el("p", { class: "hint" }, t("Ключ и модель хранятся в вашей учётной записи. По снимку сервис записывает скрытый текст и ищет по нему. В карточке этот текст не показывается.")),
      el("label", {}, [t("API-ключ Grok"), apiKey]),
      el("label", {}, [t("Модель"), model]),
      el("div", { class: "dialog-actions" }, [el("button", { type: "submit", class: "primary" }, t("Сохранить"))]),
      error,
    ]));
  }

  async function totpSection(body) {
    body.append(el("p", { class: "muted" }, t("Проверяю…")));
    let user;
    let enabled;
    try {
      user = await signedInUser();
      enabled = await totpEnabled(user);
    } catch (error) {
      body.replaceChildren(el("p", { class: "error" }, authMessage(error)));
      return;
    }
    draw();

    function draw() {
      body.replaceChildren(
        el("p", { class: "totp-status" }, [
          el("strong", {}, enabled ? t("Включён.") : t("Выключен.")),
          " ",
          enabled
            ? t("После пароля инвентарь спрашивает код из приложения-аутентификатора.")
            : t("Можно включить: после пароля инвентарь будет спрашивать шестизначный код из приложения-аутентификатора, например Google Authenticator, Microsoft Authenticator, 1Password или Aegis."),
        ]),
        el("div", { class: "dialog-actions start" }, enabled
          ? [
            el("button", { type: "button", onclick: () => setup() }, t("Подключить другое приложение")),
            el("button", { type: "button", class: "danger", onclick: () => disable() }, t("Выключить")),
          ]
          : [el("button", { type: "button", class: "primary", onclick: () => setup() }, t("Включить"))]),
        el("p", { class: "hint" }, t("Если телефон потерян, администратор может сбросить вход в два шага в разделе «Пользователи».")),
      );
    }

    async function refresh() {
      try {
        enabled = await totpEnabled(user);
      } catch (error) {
        toast(authMessage(error));
      }
      draw();
    }

    async function disable() {
      if (!window.confirm(t("Выключить вход в два шага? Входить можно будет по одному паролю."))) return;
      try {
        await setTotp(user, false);
        toast(t("Вход в два шага выключен"));
      } catch (error) {
        toast(authMessage(error));
      }
      await refresh();
    }

    // Cognito drops the old app as soon as a new secret is issued, so until the
    // new code is confirmed the account signs in with the password alone.
    async function setup() {
      let secret;
      try {
        secret = await newTotpSecret(user);
      } catch (error) {
        toast(authMessage(error));
        return;
      }
      const issuer = window.location.hostname;
      const uri = "otpauth://totp/" + encodeURIComponent(issuer + ":" + state.email)
        + "?secret=" + secret + "&issuer=" + encodeURIComponent(issuer);
      const error = el("p", { class: "error", role: "alert" });
      const code = el("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: "6", required: true });
      body.replaceChildren(
        el("ol", { class: "totp-steps" }, [
          el("li", {}, t("Отсканируйте QR-код приложением-аутентификатором.")),
          el("li", {}, t("Введите код, который покажет приложение.")),
        ]),
        qrCode(uri),
        el("p", { class: "hint" }, [t("Если отсканировать не получается, введите в приложении ключ:"), " ", el("code", { class: "totp-secret" }, secret.replace(/(.{4})/g, "$1 ").trim())]),
        el("form", {
          onsubmit: async (event) => {
            event.preventDefault();
            try {
              await verifyTotp(user, code.value.trim());
              await setTotp(user, true);
              toast(t("Вход в два шага включён"));
              await refresh();
            } catch (err) {
              error.textContent = authMessage(err);
              code.value = "";
              code.focus();
            }
          },
        }, [
          el("label", {}, [t("Код из приложения"), code]),
          el("div", { class: "dialog-actions" }, [
            el("button", { type: "button", onclick: () => refresh() }, t("Отмена")),
            el("button", { type: "submit", class: "primary" }, t("Подтвердить")),
          ]),
          error,
        ]),
      );
      code.focus();
    }
  }

  function secretNote(email, password, kind) {
    const text = {
      emailed: t("Приглашение отправлено на {email}. Если письмо не придёт, передайте временный пароль сами:", { email }),
      created: t("Пользователь {email} создан, письмо не отправлялось. Передайте ему временный пароль:", { email }),
      reset: t("Новый временный пароль для {email}:", { email }),
    }[kind];
    return el("div", { class: "secret-note", role: "status" }, [
      el("p", {}, text),
      el("code", {}, password),
      el("p", { class: "hint" }, t("Пароль показан один раз. При первом входе его нужно сменить.")),
    ]);
  }

  async function usersSection(body) {
    const note = el("div");
    const list = el("div", { class: "table-wrap" }, el("p", { class: "muted" }, t("Загружаю пользователей…")));
    const email = el("input", { type: "email", required: true, placeholder: t("почта@example.com"), "aria-label": t("Почта нового пользователя") });
    const admin = el("input", { type: "checkbox" });
    const invite = el("form", {
      class: "invite",
      onsubmit: async (event) => {
        event.preventDefault();
        try {
          const created = await api("/admin/users", { method: "POST", body: { email: email.value, admin: admin.checked } });
          note.replaceChildren(secretNote(created.email, created.temporary_password, created.emailed === false ? "created" : "emailed"));
          email.value = "";
          admin.checked = false;
          await load();
        } catch (error) {
          toast(error.message);
        }
      },
    }, [
      email,
      el("label", { class: "check" }, [admin, t("администратор")]),
      el("button", { type: "submit", class: "primary" }, t("Пригласить")),
    ]);
    body.append(invite, note, list);

    async function change(user, changes, question) {
      if (question && !window.confirm(question)) return;
      try {
        const result = await api("/admin/users", { method: "PUT", body: Object.assign({ email: user.email }, changes) });
        if (result.temporary_password) note.replaceChildren(secretNote(user.email, result.temporary_password, "reset"));
        await load();
      } catch (error) {
        toast(error.message);
      }
    }

    async function remove(user) {
      if (!window.confirm(t("Удалить пользователя {email}? Коробки и история останутся, его настройки удалятся.", { email: user.email }))) return;
      try {
        await api("/admin/users?email=" + encodeURIComponent(user.email), { method: "DELETE" });
        toast(t("Пользователь удалён"));
        await load();
      } catch (error) {
        toast(error.message);
      }
    }

    async function load() {
      let users;
      try {
        users = (await api("/admin/users")).items;
      } catch (error) {
        list.replaceChildren(el("p", { class: "error" }, error.message));
        return;
      }
      list.replaceChildren(el("table", { class: "users" }, [
        el("thead", {}, el("tr", {}, [t("Почта"), t("Статус"), "2FA", t("Админ"), ""].map((text) => el("th", {}, text)))),
        el("tbody", {}, users.map((user) => el("tr", { class: user.enabled ? "" : "disabled" }, [
          el("td", {}, [user.email, user.self ? el("span", { class: "muted" }, " " + t("(вы)")) : null]),
          el("td", {}, user.enabled ? USER_STATUS[user.status] || user.status : t("Отключён")),
          el("td", { class: user.mfa ? "" : "muted" }, [el("span", { class: "narrow-only" }, "2FA: "), user.mfa ? t("включён") : t("нет")]),
          el("td", {}, el("label", { class: "admin-toggle" }, [
            el("input", {
              type: "checkbox",
              checked: user.admin,
              disabled: user.self,
              "aria-label": t("Администратор {email}", { email: user.email }),
              onchange: (event) => change(user, { admin: event.target.checked }),
            }),
            el("span", { class: "narrow-only" }, t("администратор")),
          ])),
          el("td", { class: "user-actions" }, user.self ? null : [
            el("button", { type: "button", onclick: () => change(user, { reset_password: true }, t("Выдать {email} новый временный пароль? Старый перестанет работать.", { email: user.email })) }, t("Новый пароль")),
            user.mfa ? el("button", { type: "button", onclick: () => change(user, { reset_mfa: true }, t("Сбросить вход в два шага у {email}? Входить можно будет по одному паролю, приложение подключается заново в настройках.", { email: user.email })) }, t("Сбросить 2FA")) : null,
            el("button", { type: "button", onclick: () => change(user, { enabled: !user.enabled }) }, user.enabled ? t("Отключить") : t("Включить")),
            el("button", { type: "button", class: "danger", onclick: () => remove(user) }, t("Удалить")),
          ]),
        ]))),
      ]));
    }
    await load();
    email.focus();
  }

  function formatWhen(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(I18n.locale);
  }

  window.addEventListener("popstate", () => {
    const number = numberFromLocation();
    if (number) openBox(number, "none");
    else {
      state.selected = null;
      state.detail = null;
      state.history = [];
      setRoute(null, "none");
      renderCatalog();
      renderDetail();
    }
  });

  document.addEventListener("paste", (event) => {
    const file = imageFromClipboard(event.clipboardData);
    if (!file) return;
    const field = event.target && event.target.closest && event.target.closest("textarea, input:not([type=file])");
    if (field && event.clipboardData.getData("text/plain")) return;
    if (state.editing && state.selected && state.detail && !(event.target && event.target.closest && event.target.closest("form.composer"))) {
      event.preventDefault();
      saveCardPhoto(file);
      return;
    }
    const input = photoInputForPaste();
    if (!input) {
      toast(t("Откройте карточку, чтобы вставить фотографию"));
      return;
    }
    event.preventDefault();
    try {
      putPhotoFile(input, file);
    } catch (error) {
      toast(error.message);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.lightbox >= 0) {
        closeLightbox();
        return;
      }
      const dialog = document.querySelector(".dialog-wrap");
      if (dialog) {
        dialog.remove();
        return;
      }
      if (state.selected && !(event.target && event.target.closest && event.target.closest("textarea, input"))) {
        if (state.editing) {
          state.editing = false;
          renderDetail();
        } else {
          closeDetail();
        }
      }
      return;
    }
    if (event.target.closest("input, textarea")) return;
    if (event.key === "ArrowRight" && state.lightbox >= 0) stepLightbox(1);
    if (event.key === "ArrowLeft" && state.lightbox >= 0) stepLightbox(-1);
  });

  async function boot() {
    document.title = t("Инвентарь коробок");
    if (!window.AmazonCognitoIdentity || !config.userPoolId || !config.clientId) {
      root.textContent = t("Не загрузилась авторизация. Обновите страницу.");
      return;
    }
    try {
      applySession(await freshSession());
      renderApp();
      await loadBoxes(false);
      const number = numberFromLocation();
      if (number) await openBox(number, "none");
    } catch (error) {
      showLogin();
    }
  }

  boot();
})();
