"""「最近 20 筆紀錄」視窗：看每一句從原始辨識到最後打出的過程，可以複製或直接加入詞庫。"""

import logging

from .add_word_dialog import _bring_to_front, open_add_word_dialog
from .tkui import FONT

log = logging.getLogger(__name__)

_open_window = None


def open_history_window(ui, records, on_saved=None):
    """records：由新到舊的紀錄列表。必須在 UIThread 裡呼叫。"""
    global _open_window
    import tkinter as tk
    from tkinter import ttk

    if _open_window is not None and _open_window.winfo_exists():
        _open_window.destroy()

    px = ui.px
    win = tk.Toplevel(ui.root)
    _open_window = win
    win.title("語音輸入 - 最近 20 筆紀錄")
    win.geometry(f"{px(760)}x{px(500)}")
    frame = ttk.Frame(win, padding=px(12))
    frame.pack(fill="both", expand=True)

    style = ttk.Style(win)
    style.configure("History.Treeview", font=(FONT, 10), rowheight=px(26))
    style.configure("History.Treeview.Heading", font=(FONT, 10, "bold"))

    columns = ("time", "app", "final")
    tree = ttk.Treeview(frame, columns=columns, show="headings", height=10, style="History.Treeview")
    for col, title, width in (("time", "時間", 150), ("app", "程式", 130), ("final", "打出來的文字", 440)):
        tree.heading(col, text=title)
        tree.column(col, width=px(width), anchor="w", stretch=(col == "final"))
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")

    detail = tk.Text(frame, height=6, font=(FONT, 10), wrap="word", relief="solid", bd=1, bg="#f8f8f8")
    detail.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(px(10), px(10)))
    detail.configure(state="disabled")

    frame.rowconfigure(0, weight=3)
    frame.rowconfigure(1, weight=1)
    frame.columnconfigure(0, weight=1)

    for i, r in enumerate(records):
        final_one_line = r.get("final", "").replace("\n", " ⏎ ")
        tree.insert("", "end", iid=str(i), values=(r.get("time", ""), r.get("app", ""), final_one_line))

    def selected():
        sel = tree.selection()
        return records[int(sel[0])] if sel else None

    def show_detail(_event=None):
        r = selected()
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        if r:
            ai = "（有用 AI 校正）" if r.get("used_ai") else "（沒有用 AI 校正）"
            detail.insert("end",
                          f"原始辨識：{r.get('raw', '')}\n"
                          f"詞庫替換後：{r.get('after_dict', '')}\n"
                          f"AI 校正後：{r.get('after_ai', '')} {ai}\n"
                          f"最後打出：{r.get('final', '')}\n"
                          f"場景：{r.get('scene', '')}")
        detail.configure(state="disabled")

    def copy_final():
        r = selected()
        if r:
            win.clipboard_clear()
            win.clipboard_append(r.get("final", ""))
            status.configure(text="已複製")

    def add_word():
        r = selected()
        if r:
            open_add_word_dialog(ui, r, on_saved=on_saved)

    tree.bind("<<TreeviewSelect>>", show_detail)
    tree.bind("<Double-1>", lambda e: add_word())

    buttons = ttk.Frame(frame)
    buttons.grid(row=2, column=0, columnspan=2, sticky="we")
    status = ttk.Label(buttons, text="雙擊某一句可以直接把它加入詞庫" if records else "還沒有任何紀錄",
                       font=(FONT, 9), foreground="#666666")
    status.pack(side="left")
    ttk.Button(buttons, text="關閉", command=win.destroy).pack(side="right")
    ttk.Button(buttons, text="這句加入詞庫…", command=add_word).pack(side="right", padx=(0, px(8)))
    ttk.Button(buttons, text="複製打出來的文字", command=copy_final).pack(side="right", padx=(0, px(8)))

    if records:
        tree.selection_set("0")
        tree.focus("0")
    win.bind("<Escape>", lambda e: win.destroy())
    _bring_to_front(win)
    return win
