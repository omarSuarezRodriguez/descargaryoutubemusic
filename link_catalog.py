"""
Catálogo de enlaces + nombres y ventana para copiar (uno o todos).
No afecta calidad ni descarga; solo listado/clipboard.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


class LinkCatalog:
    """Lista thread-safe de (nombre, url), sin duplicar por URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[tuple[str, str]] = []  # (nombre, url)
        self._urls: set[str] = set()

    def add(self, name: str, url: str) -> None:
        name = (name or "").strip() or "(sin nombre)"
        url = (url or "").strip()
        if not url:
            return
        with self._lock:
            if url in self._urls:
                # Actualizar nombre si ya estaba
                for i, (n, u) in enumerate(self._items):
                    if u == url:
                        if n != name:
                            self._items[i] = (name, url)
                        break
                return
            self._items.append((name, url))
            self._urls.add(url)

    def snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._urls.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def format_entry(name: str, url: str) -> str:
    return f"{name}\t{url}"


def format_all(items: list[tuple[str, str]]) -> str:
    return "\n".join(format_entry(n, u) for n, u in items)


def refresh_link_catalog_window(parent: tk.Misc) -> None:
    win = getattr(parent, "_link_catalog_win", None)
    if win is None:
        return
    try:
        if win.winfo_exists():
            _refresh_listbox(win)
    except tk.TclError:
        pass


def show_link_catalog_window(parent: tk.Misc, catalog: LinkCatalog) -> None:
    """Abre (o enfoca) una ventana para ver/copiar el listado."""
    existing = getattr(parent, "_link_catalog_win", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                _refresh_listbox(existing)
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

    win = tk.Toplevel(parent)
    win.title("Listado de enlaces y nombres")
    win.geometry("720x420")
    win.minsize(480, 280)
    parent._link_catalog_win = win  # type: ignore[attr-defined]

    frame = ttk.Frame(win, padding=12)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(
        frame,
        text="Nombre y enlace de cada pista (se rellena al resolver/descargar). "
        "Copia uno o todos:",
    ).pack(anchor=tk.W, pady=(0, 8))

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)

    scroll = ttk.Scrollbar(list_frame)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    listbox = tk.Listbox(
        list_frame,
        selectmode=tk.EXTENDED,
        yscrollcommand=scroll.set,
        font=("Consolas", 10),
    )
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.configure(command=listbox.yview)

    win._catalog = catalog  # type: ignore[attr-defined]
    win._listbox = listbox  # type: ignore[attr-defined]

    def copy_text(text: str, label: str) -> None:
        if not text.strip():
            messagebox.showinfo("Vacío", "No hay nada que copiar.", parent=win)
            return
        win.clipboard_clear()
        win.clipboard_append(text)
        win.update_idletasks()
        messagebox.showinfo("Copiado", f"{label} al portapapeles.", parent=win)

    def copy_selected() -> None:
        sel = listbox.curselection()
        items = catalog.snapshot()
        if not sel:
            messagebox.showinfo(
                "Selección",
                "Selecciona una o más filas (clic o Ctrl+clic).",
                parent=win,
            )
            return
        lines = [format_entry(items[i][0], items[i][1]) for i in sel if i < len(items)]
        copy_text("\n".join(lines), f"{len(lines)} línea(s) copiada(s)")

    def copy_all() -> None:
        items = catalog.snapshot()
        copy_text(format_all(items), f"Todo ({len(items)} línea(s))")

    def copy_one_event(_event=None) -> None:
        sel = listbox.curselection()
        if len(sel) == 1:
            items = catalog.snapshot()
            i = sel[0]
            if i < len(items):
                copy_text(format_entry(items[i][0], items[i][1]), "1 línea copiada")

    def save_file() -> None:
        items = catalog.snapshot()
        if not items:
            messagebox.showinfo("Vacío", "El listado está vacío.", parent=win)
            return
        path = filedialog.asksaveasfilename(
            parent=win,
            title="Guardar listado",
            defaultextension=".txt",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
            initialfile="listado_enlaces.txt",
        )
        if not path:
            return
        Path(path).write_text(format_all(items) + "\n", encoding="utf-8")
        messagebox.showinfo("Guardado", f"Guardado en:\n{path}", parent=win)

    def clear_list() -> None:
        if not catalog.snapshot():
            return
        if messagebox.askyesno(
            "Limpiar",
            "¿Vaciar el listado de esta sesión?",
            parent=win,
        ):
            catalog.clear()
            _refresh_listbox(win)

    btn_row = ttk.Frame(frame)
    btn_row.pack(fill=tk.X, pady=(10, 0))
    ttk.Button(btn_row, text="Copiar seleccionado", command=copy_selected).pack(
        side=tk.LEFT
    )
    ttk.Button(btn_row, text="Copiar todos", command=copy_all).pack(
        side=tk.LEFT, padx=(8, 0)
    )
    ttk.Button(btn_row, text="Guardar .txt…", command=save_file).pack(
        side=tk.LEFT, padx=(8, 0)
    )
    ttk.Button(btn_row, text="Limpiar", command=clear_list).pack(
        side=tk.LEFT, padx=(8, 0)
    )
    ttk.Button(btn_row, text="Actualizar", command=lambda: _refresh_listbox(win)).pack(
        side=tk.LEFT, padx=(8, 0)
    )
    ttk.Button(btn_row, text="Cerrar", command=win.destroy).pack(side=tk.RIGHT)

    listbox.bind("<Double-Button-1>", copy_one_event)

    def on_close() -> None:
        parent._link_catalog_win = None  # type: ignore[attr-defined]
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)
    _refresh_listbox(win)


def _refresh_listbox(win: tk.Toplevel) -> None:
    catalog: LinkCatalog = win._catalog  # type: ignore[attr-defined]
    listbox: tk.Listbox = win._listbox  # type: ignore[attr-defined]
    listbox.delete(0, tk.END)
    for name, url in catalog.snapshot():
        # Mostrar legible; al copiar se usa tab nombre\turl
        listbox.insert(tk.END, f"{name}  |  {url}")
