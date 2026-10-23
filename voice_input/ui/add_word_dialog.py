"""「把上一句加入詞庫」對話框：顯示原始辨識和最後打出的字，讓使用者填「聽錯的字 → 正確的字」。

存進 dictionary.yaml 的 replacements（保留檔案原本的註解），詞庫會自動更新，下一句就生效。
"""

import logging

from ..dictionary import add_to_dictionary
from ..paths import DICTIONARY_PATH
from .tkui import FONT

log = logging.getLogger(__name__)

_open_dialog = None   # 同一時間只開一個


def _bring_to_front(win):
    """從系統匣叫出來的視窗常會被壓在其他視窗後面，先暫時置頂再取消，確保看得到。"""
    win.lift()
    win.attributes("-topmost", True)
    win.after(300, lambda: win.attributes("-topmost", False))
    win.focus_force()


def open_add_word_dialog(ui, record, dictionary_path=DICTIONARY_PATH, on_saved=None,
                         wrong="", right="", labels=None, add_vocabulary=True):
    """record：最近紀錄的一筆（dict），None 代表還沒有任何紀錄。必須在 UIThread 裡呼叫。

    wrong／right：預先填好的「聽錯的字」「正確的字」（詞庫自動學習按「修改…」時用）。
    labels：上面兩個方框的標題，預設是「原始辨識」和「最後打出來的」。
    add_vocabulary：「也加入專有名詞」預設要不要勾。
    """
    global _open_dialog
    import tkinter as tk
    from tkinter import messagebox, ttk

    if record is None:
        messagebox.showinfo("把上一句加入詞庫", "還沒有任何語音輸入紀錄。\n先說一句話，再回來這裡。", parent=ui.root)
        return
    if _open_dialog is not None and _open_dialog.winfo_exists():
        _open_dialog.destroy()

    px = ui.px
    win = tk.Toplevel(ui.root)
    _open_dialog = win
    win.title("把上一句加入詞庫")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=px(16))
    frame.pack(fill="both", expand=True)
    label_font = (FONT, 10)
    text_font = (FONT, 11)

    def readonly_box(parent, value, row, title):
        ttk.Label(parent, text=title, font=label_font).grid(row=row, column=0, sticky="nw", pady=(0, px(2)))
        box = tk.Text(parent, height=2, width=46, font=text_font, wrap="word", relief="solid", bd=1,
                      bg="#f5f5f5")
        box.insert("1.0", value)
        box.configure(state="disabled")
        box.grid(row=row + 1, column=0, columnspan=2, sticky="we", pady=(0, px(10)))

    top_label, bottom_label = labels or ("辨識引擎聽到的（原始辨識）：", "最後打出來的：")
    readonly_box(frame, record.get("raw", ""), 0, top_label)
    readonly_box(frame, record.get("final", ""), 2, bottom_label)

    ttk.Label(frame, text="聽錯的字（可以從上面選取複製）：", font=label_font).grid(row=4, column=0, sticky="w")
    wrong_var = tk.StringVar(value=wrong)
    wrong_entry = ttk.Entry(frame, textvariable=wrong_var, font=text_font, width=46)
    wrong_entry.grid(row=5, column=0, columnspan=2, sticky="we", pady=(px(2), px(8)))

    ttk.Label(frame, text="正確的字：", font=label_font).grid(row=6, column=0, sticky="w")
    right_var = tk.StringVar(value=right)
    right_entry = ttk.Entry(frame, textvariable=right_var, font=text_font, width=46)
    right_entry.grid(row=7, column=0, columnspan=2, sticky="we", pady=(px(2), px(8)))

    vocab_var = tk.BooleanVar(value=add_vocabulary)
    ttk.Checkbutton(
        frame, variable=vocab_var,
        text="也把「正確的字」加入專有名詞（讓辨識引擎下次更容易直接聽對）",
    ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, px(8)))

    hint = ttk.Label(
        frame, font=(FONT, 9), foreground="#666666", wraplength=px(420), justify="left",
        text="例：原始辨識是「我在用拜加溫」，聽錯的字填「拜加溫」，正確的字填「buy+1」。\n"
             "之後聽到「拜加溫」或唸起來很像的字，都會自動換成 buy+1。",
    )
    hint.grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, px(10)))

    status = ttk.Label(frame, text="", font=label_font)
    status.grid(row=10, column=0, sticky="w")

    def save(_event=None):
        wrong, right = wrong_var.get().strip(), right_var.get().strip()
        if not wrong or not right:
            status.configure(text="「聽錯的字」和「正確的字」都要填。", foreground="#c0392b")
            return
        if wrong == right:
            status.configure(text="兩個一樣，不用加。", foreground="#c0392b")
            return
        if len(wrong) == 1 and not messagebox.askyesno(
            "只有一個字", f"「{wrong}」只有一個字，所有出現「{wrong}」的地方都會被換掉，確定要加嗎？", parent=win
        ):
            return
        try:
            added = add_to_dictionary(
                dictionary_path,
                replacements=[(wrong, right)],
                vocabulary=[right] if vocab_var.get() else [],
            )
        except Exception as e:
            log.exception("加入詞庫失敗")
            status.configure(text=f"沒有加成功：{e}", foreground="#c0392b")
            return
        if added == 0:
            status.configure(text="詞庫裡已經有這個詞了。", foreground="#c0392b")
            return
        status.configure(text="✓ 已加入詞庫，下一句就會生效。", foreground="#27ae60")
        if on_saved:
            on_saved(wrong, right)
        win.after(1200, win.destroy)

    buttons = ttk.Frame(frame)
    buttons.grid(row=10, column=1, sticky="e")
    ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right")
    ttk.Button(buttons, text="加入詞庫", command=save).pack(side="right", padx=(0, px(8)))

    win.bind("<Return>", save)
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    # 放在螢幕中間
    x = (win.winfo_screenwidth() - win.winfo_width()) // 2
    y = (win.winfo_screenheight() - win.winfo_height()) // 3
    win.geometry(f"+{x}+{y}")
    _bring_to_front(win)
    wrong_entry.focus_set()
    return win
