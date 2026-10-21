# WordPro

**A retro-inspired terminal word processor for Linux, written in Python.**

WordPro brings the feel of a classic dedicated word processor to a modern Linux terminal. With its amber-on-dark interface, keyboard-driven menus, document formatting, and built-in proofreading tools, it is designed for distraction-free writing without the overhead of a full office suite.

## Features

- **Retro terminal interface** — an amber-on-dark display inspired by classic word processors.
- **RTF and plain-text support** — open and edit `.rtf` and `.txt` documents, with RTF as the default save format.
- **Keyboard-driven editing** — navigate documents, edit text, and access menus without relying on a mouse.
- **Automatic line wrapping** — text reflows as you edit.
- **Document formatting** — Normal and Heading 1–3 paragraph styles, with adjustable font sizes.
- **Page layout** — A4, A5, Standard, Normal, and Wide layouts, adjustable margins, and page breaks.
- **Search and replace** — find text and make changes throughout a document.
- **Spell checking** — Hunspell integration with a bundled English (US) dictionary, plus support for importing other Hunspell dictionaries.
- **Grammar checking** — built-in checks for common grammatical errors, with optional integration with a local LanguageTool server.
- **Writing tools** — a built-in thesaurus, word count, text alignment, and adjustable text widths.
- **Headers and footers** — add document header and footer lines, with page numbering.
- **Print preview** — inspect document layout before printing.
- **Recovery drafts** — recover unsaved work after an interrupted session.
- **Document backups** — `.old` backups help protect existing files when recovering drafts.

## Screenshots

Screenshots of WordPro will be added here.

## Requirements

- Linux
- Python 3 with `curses` support
- A terminal emulator

Optional components:

- **Hunspell** — for dictionary-based spell checking.
- **Java 17 or newer** — for running the optional standalone LanguageTool server.
- **curl or wget and unzip** — for downloading and installing LanguageTool through the installer.

WordPro can run without Hunspell or LanguageTool; its built-in proofreading checks remain available.

## Installation

### Run from source

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/WordPro.git
cd WordPro
```

Launch WordPro:

```bash
python3 wordpro.py
```

Replace `YOUR_USERNAME` with the GitHub account that hosts this repository.

### Install for your user

Run the included installer:

```bash
chmod +x install.sh
./install.sh --user
```

This installs WordPro for your account and adds an application menu entry. You can launch it from your desktop environment or run:

```bash
wordpro
```

### Install system-wide

To install WordPro for all users:

```bash
sudo ./install.sh --system
```

### Optional LanguageTool integration

To install WordPro with a local LanguageTool server:

```bash
./install.sh --user --with-languagetool
```

This downloads the standalone LanguageTool server, approximately 252 MB according to the installer documentation. Java 17 or newer is required.

The server runs locally on `127.0.0.1:8081`. WordPro can also connect to an existing local LanguageTool server by setting `WORDPRO_LANGUAGETOOL_URL`.

### Arch Linux and CachyOS

An Arch User Repository (AUR) package is planned.

Once published, the intended installation command will be:

```bash
yay -S wordpro
```

The package will be available after its AUR submission is published and the packaging has been tested.

## Usage

Start WordPro from a terminal or your applications menu.

On startup, WordPro lists `.rtf` and `.txt` files in the current folder. Use the filter field to find a document, select it with the arrow keys, and press Enter to open it. Press Esc to start a new document.

Useful keyboard shortcuts:

| Shortcut | Action |
|---|---|
| `Ctrl+S` | Save the document |
| `Ctrl+O` | Open a document |
| `Ctrl+Enter` | Insert a page break |
| `Page Up` / `Page Down` | Move by one visible page |
| `F1`–`F10` | Select a menu |
| `Ctrl+D` or `Ctrl+C` | Start the quit confirmation |

The menus provide access to formatting, scrolling, searching, proofreading, forms, page layout, printing, and block editing.

New documents are saved as numbered `Document N.rtf` files in the current folder.

## Data and recovery

WordPro stores recovery drafts and document state under:

```text
~/.local/state/wordpro
```

If `XDG_STATE_HOME` is set, WordPro uses that directory instead.

When quitting, you can save your work, discard the current changes while retaining a recovery draft, or cancel quitting. Recovery and backup behaviour is designed to help prevent accidental loss of existing documents.

## Uninstalling

For a per-user installation, run the installer again from the project directory:

```bash
./install.sh --user --uninstall
```

For a system-wide installation:

```bash
sudo ./install.sh --system --uninstall
```

The installer removes the application and launcher while retaining your documents and recovery data in your home directory.

## Project status

WordPro is an evolving project. Features and behaviour may change as development continues.

Contributions, bug reports, suggestions, and testing feedback are welcome.

## Contributing

If you encounter a bug or have an idea for improving WordPro, please open an issue on GitHub.

Pull requests are welcome. When reporting a bug, include your Linux distribution, Python version, steps to reproduce the problem, and any relevant error messages.

## Licence

The project licence will be specified here once the repository's licence has been confirmed. The bundled English (US) Hunspell dictionary has its own licence in `dictionaries/LICENSE.txt`.
