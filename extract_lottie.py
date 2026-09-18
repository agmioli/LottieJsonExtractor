# -*- coding: utf-8 -*-
"""
Lottie-json Extractor — GUI.

Сборка в .exe:
    pip install playwright pyinstaller
    set PLAYWRIGHT_BROWSERS_PATH=0
    playwright install chromium
    pyinstaller --onefile --windowed --name LottieJsonExtractor ^
                --icon app_icon.ico ^
                --add-data "app_icon.ico;." ^
                --collect-all playwright ^
                lottie_extractor.py
"""

import asyncio
import ctypes
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from playwright.async_api import async_playwright

# =====================================================================
#                              Пути
# =====================================================================
if getattr(sys, "frozen", False):
    BASE_DIR      = os.path.dirname(sys.executable)
    RESOURCES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
    RESOURCES_DIR = BASE_DIR

def resource_path(name: str) -> str:
    return os.path.join(RESOURCES_DIR, name)

ICON_PATH     = resource_path("app_icon.ico")
USER_DATA_DIR = os.path.join(BASE_DIR, "vk_profile")
SAVE_DIR      = os.path.join(BASE_DIR, "lottie_out")
os.makedirs(SAVE_DIR, exist_ok=True)

DEFAULT_URL = (
    "https://vk.ru/id737474718"
    "?w=%2Fgifts_catalog%3Frecipient_ids%3D737474718%26ref%3Dprofile_button"
)

# =====================================================================
#                   Защита от повторного запуска
# =====================================================================
MUTEX_NAME = "LottieJsonExtractor_SingleInstance_v1"
_instance_mutex_handle = None   # держим хендл — иначе GC его освободит


def acquire_single_instance() -> bool:
    """
    True  — мы единственный экземпляр.
    False — уже запущено другое окно приложения.
    На не-Windows всегда True (для отладки достаточно).
    """
    global _instance_mutex_handle
    if sys.platform != "win32":
        return True
    try:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype  = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
        ]
        ERROR_ALREADY_EXISTS = 183
        _instance_mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


# =====================================================================
#                         Инжект-скрипт
# =====================================================================
INIT_SCRIPT = r"""
(() => {
    if (window.__lottieHooked) return;
    window.__lottieHooked = true;

    function looksLikeLottie(obj) {
        return obj && typeof obj === 'object'
            && ('layers' in obj) && ('v' in obj)
            && ('fr' in obj) && ('ip' in obj);
    }

    function send(obj, source) {
        try {
            if (looksLikeLottie(obj) && typeof window.lottieSaver === 'function') {
                window.lottieSaver(source, JSON.stringify(obj));
                console.log('[LOTTIE-CAPTURE] -> sent, layers=',
                            (obj.layers || []).length);
            }
        } catch (e) {
            console.log('[LOTTIE-CAPTURE] send error:', e.message);
        }
    }

    const origParse = JSON.parse;
    JSON.parse = function(t, r) {
        const v = origParse.call(this, t, r);
        send(v, 'JSON.parse');
        return v;
    };

    if (window.Response && Response.prototype.json) {
        const origJson = Response.prototype.json;
        Response.prototype.json = function() {
            return origJson.call(this).then(d => { send(d, 'Response.json'); return d; });
        };
    }

    if (window.fetch) {
        const origFetch = window.fetch;
        window.fetch = function(...a) {
            return origFetch.apply(this, a).then(resp => {
                try {
                    const c = resp.clone();
                    c.text().then(t => {
                        try { send(origParse.call(JSON, t), 'fetch'); } catch(e){}
                    }).catch(()=>{});
                } catch(e){}
                return resp;
            });
        };
    }

    if (window.XMLHttpRequest) {
        const oOpen = XMLHttpRequest.prototype.open;
        const oSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m,u,...r){ this.__url=u; return oOpen.call(this,m,u,...r); };
        XMLHttpRequest.prototype.send = function(...a){
            this.addEventListener('load', function(){
                try {
                    const t = this.responseText;
                    if (!t) return;
                    send(origParse.call(JSON, t), 'XHR:' + (this.__url||''));
                } catch(e){}
            });
            return oSend.apply(this, a);
        };
    }

    console.log('[LOTTIE-CAPTURE] hooked');
})();
"""


# =====================================================================
#                          Иконка окна
# =====================================================================
def apply_window_icon(root: tk.Tk):
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "LottieJsonExtractor.App.1"
            )
        except Exception:
            pass

    if os.path.isfile(ICON_PATH):
        try:
            root.iconbitmap(default=ICON_PATH)
        except Exception:
            try:
                root.iconbitmap(ICON_PATH)
            except Exception:
                pass
        try:
            img = tk.PhotoImage(file=ICON_PATH)
            root.iconphoto(True, img)
            root._icon_ref = img
        except Exception:
            pass


# =====================================================================
#                                 GUI
# =====================================================================
class LottieApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Lottie-json Extractor")
        root.geometry("920x680")
        root.minsize(720, 520)

        apply_window_icon(root)

        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()

        # --- события воркеров ---
        self.login_event = threading.Event()      # воркер ждёт «я вошёл»
        self.stop_event  = threading.Event()      # остановить воркер
        self.login_stop_event = threading.Event() # закрыть отдельное окно входа

        self.worker: threading.Thread | None = None           # извлечение
        self.login_worker: threading.Thread | None = None     # окно входа

        self.url_locked = False

        self._build_ui()
        self._poll_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------ UI ------------------------
    def _build_ui(self):
        top = ttk.LabelFrame(
            self.root,
            text="Адрес страницы, с которой нужно собрать анимации",
            padding=8,
        )
        top.pack(fill=tk.X, padx=10, pady=(10, 4))

        row = ttk.Frame(top)
        row.pack(fill=tk.X)

        self.url_var = tk.StringVar(value=DEFAULT_URL)
        self.url_entry = ttk.Entry(
            row, textvariable=self.url_var, font=("Segoe UI", 10)
        )
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.url_entry.focus_set()

        self.ok_btn = ttk.Button(row, text="OK", width=12,
                                 command=self.on_confirm_url)
        self.ok_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ---- Меню + хоткеи для поля URL ----
        self._url_menu = tk.Menu(self.root, tearoff=0)
        self._url_menu.add_command(
            label="Вырезать",
            command=lambda: self.url_entry.event_generate("<<Cut>>"),
        )
        self._url_menu.add_command(
            label="Копировать",
            command=lambda: self.url_entry.event_generate("<<Copy>>"),
        )
        self._url_menu.add_command(label="Вставить", command=self._paste_url)
        self._url_menu.add_separator()
        self._url_menu.add_command(label="Выделить всё",
                                   command=self._select_all_url)

        self.url_entry.bind("<Button-3>", self._show_url_menu)
        self.url_entry.bind("<Control-v>", self._paste_url)
        self.url_entry.bind("<Control-V>", self._paste_url)
        self.url_entry.bind("<Control-c>",
                            lambda e: self.url_entry.event_generate("<<Copy>>"))
        self.url_entry.bind("<Control-C>",
                            lambda e: self.url_entry.event_generate("<<Copy>>"))
        self.url_entry.bind("<Control-x>",
                            lambda e: self.url_entry.event_generate("<<Cut>>"))
        self.url_entry.bind("<Control-X>",
                            lambda e: self.url_entry.event_generate("<<Cut>>"))
        self.url_entry.bind("<Control-a>",
                            lambda e: (self._select_all_url(), "break")[1])
        self.url_entry.bind("<Control-A>",
                            lambda e: (self._select_all_url(), "break")[1])

        # ---- Кнопки ----
        ctrl = ttk.Frame(self.root, padding=(10, 0))
        ctrl.pack(fill=tk.X)

        self.start_btn = ttk.Button(ctrl, text="▶  Запустить",
                                    command=self.on_start)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(ctrl, text="■  Остановить",
                                   command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.login_btn = ttk.Button(
            ctrl, text="🔐  Войти в ВК/в соц.сеть",
            command=self.on_login_click,
        )
        self.login_btn.pack(side=tk.LEFT, padx=5)

        ttk.Button(ctrl, text="📁  Открыть папку результатов",
                   command=self.on_open_folder).pack(side=tk.LEFT, padx=5)

        # ---- Журнал ----
        logf = ttk.LabelFrame(self.root, text="Журнал событий", padding=5)
        logf.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        self.log_text = tk.Text(
            logf, wrap=tk.WORD, state=tk.DISABLED, height=15,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            font=("Consolas", 9),
        )
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- Статус-бар ----
        self.status_var = tk.StringVar(value="Готово к работе")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W, padding=(8, 3)
                  ).pack(fill=tk.X, side=tk.BOTTOM)

    # --------------- Работа с URL ---------------
    def _paste_url(self, event=None):
        if str(self.url_entry.cget("state")) == "readonly":
            return "break"
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        text = text.strip().replace("\r", "").replace("\n", "")
        try:
            self.url_entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        self.url_entry.insert("insert", text)
        return "break"

    def _select_all_url(self):
        self.url_entry.selection_range(0, tk.END)
        self.url_entry.icursor(tk.END)
        self.url_entry.focus_set()

    def _show_url_menu(self, event):
        try:
            self.url_entry.focus_set()
            self._url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._url_menu.grab_release()

    def on_confirm_url(self):
        if self.url_locked:
            self.url_locked = False
            self.url_entry.configure(state="normal")
            self.ok_btn.configure(text="OK")
            self.log("[*] Адрес разблокирован для редактирования.")
        else:
            url = self.url_var.get().strip()
            if not url:
                messagebox.showwarning("URL пуст", "Введите адрес страницы.")
                return
            self.url_var.set(url)
            self.url_locked = True
            self.url_entry.configure(state="readonly")
            self.ok_btn.configure(text="✎ Изменить")
            self.log(f"[+] Адрес зафиксирован: {url}")

    # --------------- Очередь UI ---------------
    def log(self, msg: str):
        self.ui_queue.put(("log", str(msg)))

    def _ui_set(self, action: str, **kw):
        self.ui_queue.put(("ui", (action, kw)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()

                if kind == "log":
                    self.log_text.configure(state=tk.NORMAL)
                    self.log_text.insert(tk.END, payload + "\n")
                    self.log_text.see(tk.END)
                    self.log_text.configure(state=tk.DISABLED)
                    self.status_var.set(payload)

                elif kind == "ui":
                    action, kw = payload
                    if action == "enable_login":
                        self.login_btn.configure(state=tk.NORMAL)
                    elif action == "disable_login":
                        self.login_btn.configure(state=tk.DISABLED)
                    elif action == "login_worker_started":
                        # На время работы окна входа блокируем запуск извлечения
                        self.start_btn.configure(state=tk.DISABLED)
                        self.login_btn.configure(state=tk.DISABLED)
                    elif action == "login_worker_done":
                        # Возвращаемся в idle
                        if not (self.worker and self.worker.is_alive()):
                            self.start_btn.configure(state=tk.NORMAL)
                            self.login_btn.configure(state=tk.NORMAL)
                    elif action == "worker_started":
                        self.start_btn.configure(state=tk.DISABLED)
                        self.stop_btn.configure(state=tk.NORMAL)
                        self.login_btn.configure(state=tk.DISABLED)
                        self.ok_btn.configure(state=tk.DISABLED)
                        self.url_entry.configure(state="readonly")
                    elif action == "worker_done":
                        self.start_btn.configure(state=tk.NORMAL)
                        self.stop_btn.configure(state=tk.DISABLED)
                        self.login_btn.configure(state=tk.NORMAL)
                        self.ok_btn.configure(state=tk.NORMAL)
                        if not self.url_locked:
                            self.url_entry.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # --------------- Кнопка «Войти в ВК» ---------------
    def on_login_click(self):
        # Режим Б: воркер ждёт подтверждения
        if self.worker and self.worker.is_alive() and not self.login_event.is_set():
            self.login_event.set()
            self.login_btn.configure(state=tk.DISABLED)
            self.log("[+] Продолжаем — вход подтверждён.")
            return

        # Режим А: отдельное окно входа
        if self.login_worker and self.login_worker.is_alive():
            return
        self._start_login_browser()

    def _start_login_browser(self):
        self.login_stop_event.clear()
        self._ui_set("login_worker_started")
        self.login_worker = threading.Thread(
            target=self._login_browser_worker, daemon=True
        )
        self.login_worker.start()

    def _login_browser_worker(self):
        try:
            asyncio.run(self._login_browser_run())
        except Exception as e:
            self.log(f"[!] Ошибка окна входа: {type(e).__name__}: {e}")
        finally:
            self._ui_set("login_worker_done")

    async def _login_browser_run(self):
        self.log("[*] Открываю окно для входа в ВК...")
        self.log("    Войдите в аккаунт, затем закройте окно браузера.")
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto("https://vk.ru/",
                                    wait_until="domcontentloaded",
                                    timeout=60000)
                except Exception as e:
                    self.log(f"[!] Не удалось открыть vk.ru: {e}")

                # Ждём, пока пользователь закроет окно или запросит выход
                while not self.login_stop_event.is_set():
                    try:
                        if not context.pages:
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.7)

                self.log("[+] Окно входа закрыто. Сессия сохранена.")
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    # --------------- Кнопки «Пуск/Стоп/Папка» ---------------
    def on_start(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("URL пуст", "Введите адрес страницы.")
            return
        if self.worker and self.worker.is_alive():
            return
        if self.login_worker and self.login_worker.is_alive():
            messagebox.showinfo(
                "Окно входа открыто",
                "Сначала закройте окно входа в ВК, потом запускайте извлечение."
            )
            return

        self.login_event.clear()
        self.stop_event.clear()

        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.status_var.set("Запуск...")

        self._ui_set("worker_started")

        self.worker = threading.Thread(
            target=self._run_worker, args=(url,), daemon=True
        )
        self.worker.start()

    def on_stop(self):
        self.stop_event.set()
        self.log("[*] Запрошена остановка (закроется после текущего шага)...")

    def on_open_folder(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(SAVE_DIR)          # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{SAVE_DIR}"')
            else:
                os.system(f'xdg-open "{SAVE_DIR}"')
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    # --------------- Закрытие ---------------
    def _on_close(self):
        # 1) Останавливаем окно входа, если открыто
        if self.login_worker and self.login_worker.is_alive():
            self.login_stop_event.set()
            self.login_worker.join(timeout=5)

        # 2) Останавливаем воркер извлечения
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.login_event.set()     # разбудить ожидание логина
            self.log("[*] Закрытие: дожидаюсь остановки браузера...")
            self.worker.join(timeout=10)

        # 3) Разрушаем окно
        try:
            self.root.destroy()
        except Exception:
            pass

    # --------------- Воркер извлечения ---------------
    def _run_worker(self, url: str):
        try:
            asyncio.run(self._extract(url))
        except Exception as e:
            self.log(f"[!] Критическая ошибка: {type(e).__name__}: {e}")
        finally:
            self._ui_set("worker_done")

    async def _extract(self, url: str):
        captured = []
        seen = set()

        self.log("[*] Запуск браузера Playwright (Chromium)...")

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()

                def on_lottie(source, source_tag, json_str):
                    try:
                        data = json.loads(json_str)
                    except Exception as e:
                        self.log(f"[!] JSON parse fail ({source_tag}): {e}")
                        return
                    n = len(data.get("layers", []))
                    h = hash(json_str)
                    if h in seen:
                        return
                    seen.add(h)
                    captured.append({"source": source_tag, "data": data})
                    self.log(f"[+] Поймана Lottie ({source_tag}) — "
                             f"layers={n}, всего={len(captured)}")

                await page.expose_binding("lottieSaver", on_lottie)
                await page.add_init_script(INIT_SCRIPT)

                page.on("console",
                        lambda m: self.log(f"[browser] {m.text}")
                        if ("LOTTIE" in m.text or "error" in m.text.lower()) else None)

                self.log("[*] Открываю vk.ru...")
                await page.goto("https://vk.ru/",
                                wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)

                logged_in = await page.evaluate(
                    "() => !document.querySelector('#index_login_form, .LoginForm')"
                )
                if not logged_in:
                    self.log("[!] Требуется вход в ВК.")
                    self.log("    Войдите в открывшемся окне браузера,")
                    self.log("    затем нажмите «Войти в ВК/в соц.сеть» в этом окне.")
                    self._ui_set("enable_login")

                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self.login_event.wait)

                    if self.stop_event.is_set():
                        self.log("[*] Отменено пользователем.")
                        return

                    self.log("[+] Продолжаем работу.")
                    await asyncio.sleep(2)
                else:
                    self.log("[+] Авторизация уже выполнена.")

                if self.stop_event.is_set():
                    return

                self.log(f"[*] Переход: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                self.log("[*] Ожидаю появления canvas[data-testid='lottie']...")
                try:
                    await page.wait_for_selector(
                        "canvas[data-testid='lottie']",
                        timeout=60000, state="attached",
                    )
                    self.log("[+] ✅ Canvas Lottie найден.")
                except Exception as e:
                    self.log(f"[!] Canvas не появился за 60 сек: {e}")

                self.log("[*] Скроллю каталог (12 шагов)...")
                for i in range(12):
                    if self.stop_event.is_set():
                        self.log("[*] Прервано пользователем.")
                        break
                    await page.mouse.wheel(0, 700)
                    await asyncio.sleep(1.2)
                    if i % 3 == 0:
                        self.log(f"    шаг {i+1}/12, поймано: {len(captured)}")
                await asyncio.sleep(3)

                if not captured:
                    self.log("[!] Ни одной анимации не поймано.")
                    return

                self.log(f"[*] Сохраняю {len(captured)} файлов в {SAVE_DIR} ...")
                captured.sort(key=lambda x: len(x["data"].get("layers", [])),
                              reverse=True)
                for i, item in enumerate(captured, start=1):
                    n = len(item["data"].get("layers", []))
                    fname = os.path.join(SAVE_DIR, f"sticker_{i:03d}_layers{n}.json")
                    with open(fname, "w", encoding="utf-8") as f:
                        json.dump(item["data"], f, ensure_ascii=False)

                self.log(f"[+] ✅ Сохранено файлов: {len(captured)}")
                self.log("=" * 55)
                self.log(f"[*] ИТОГО: {len(captured)} уникальных Lottie-анимаций")
                self.log(f"[*] Папка: {SAVE_DIR}")
                self.log("=" * 55)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass


# =====================================================================
def main():
    # ---- Защита от второго запуска ----
    if not acquire_single_instance():
        tmp = tk.Tk()
        tmp.withdraw()
        try:
            messagebox.showwarning(
                "Lottie-json Extractor",
                "Программа уже запущена.\n\n"
                "Закройте работающее окно, прежде чем запускать новое."
            )
        finally:
            tmp.destroy()
        sys.exit(0)

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    LottieApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()