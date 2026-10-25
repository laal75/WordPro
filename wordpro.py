#!/usr/bin/env python3
"""WordPro terminal word processor."""
import curses
import json
import os
import re
import shutil
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from urllib.parse import urlencode,urlsplit
from urllib.request import urlopen

# Menu bar (F1..F10; F11 is reserved by terminals for fullscreen, F12 toggles
# notes focus): FILE F1 · STYLE F2 · SCROLL F3 · SEARCH F4 · GRAMMAR F5
# (Grammar1+2 merged) · FORM F6 · CTR/RMF F7 · PAGE F8 · PRINT F9 · BLOCK F10
TABS = ["FILE", "STYLE", "SCROLL", "SEARCH", "GRAMMAR", "FORM", "CTR/RMF", "PAGE", "PRINT", "BLOCK"]
MENUS = {
    "FILE": ["Main menu", "Save", "Save As", "Adjust view", "Quit"],
    "STYLE": ["Normal", "Heading 1", "Heading 2", "Heading 3", "Font size", "Bold", "Underline", "Show spaces"],
    "BLOCK": ["Copy line", "Delete line", "Move line up", "Move line down"],
    "CTR/RMF": ["Center", "Right align", "Left align", "Justify"],
    "GRAMMAR": ["Spell check", "Grammar check", "Auto capitalisation", "Import Hunspell dictionary", "Word count", "Thesaurus"],
    "SEARCH": ["Find", "Replace"],
    "FORM": ["Insert header line", "Insert footer line"],
    "PAGE": ["Paper size", "L margin", "R margin", "Margins", "Insert page break", "Page number"],
    "PRINT": ["Print preview", "Export plain text"],
    "SCROLL": ["Scroll top", "Scroll bottom"],
}

# Terminal width (columns) at which the three-pane workspace activates.
WIDE_MIN_WIDTH=150
# Minimum width of the writing area, in columns. The margins can never shrink
# the text below this, so a margin setting (or a terminal reporting a narrow
# width mid-typing) can never collapse the wrap width into one-word-per-line
# fragments.
MIN_TEXT_WIDTH=30

class WordProcessor:
    def __init__(self, screen):
        self.s=screen; self.lines=[""]; self.line_formats=[{"heading":0,"size":10}]; self.y=self.x=self.top=self.tab=0
        self.path=None; self.name=self.next_default_document()[0].stem; self.dirty=False
        self.quit_requested=False; self.preserve_recovery=False
        self.ctrl_c_pending=False; self.in_quit_prompt=False
        self.recovered_draft=False
        state_root=Path(os.environ.get("XDG_STATE_HOME",Path.home()/".local/state"))
        self.state_dir=state_root/"wordpro"
        self.legacy_state_dir=state_root/"wp2400"
        self.recovery_file=self.state_dir/"recovery.json"
        self.position_file=self.state_dir/"positions.json"
        self.added_folders_file=self.state_dir/"folders.json"
        self.layout_file=self.state_dir/"layout.json"
        self.mounted_shares=[]
        # Adjustable three-pane workspace: left-to-right order of the panes
        # and each pane's share of the usable width (fractions of the whole).
        self.pane_order=["context","writing","notes"]
        self.pane_fractions={"context":1.0/3.0,"writing":1.0/3.0,"notes":1.0/3.0}
        self.adjust_view=False
        self.view_drag=None
        self.layout_backup=None
        try:
            if self.layout_file.is_file():
                d=json.loads(self.layout_file.read_text(encoding="utf-8"))
                order=d.get("order")
                if isinstance(order,list) and sorted(order)==["context","notes","writing"] and len(order)==3:
                    self.pane_order=list(order)
                fr=d.get("fractions")
                if isinstance(fr,dict) and all(float(fr.get(n,0))>0 for n in self.pane_order):
                    self.pane_fractions={n:float(fr[n]) for n in self.pane_order}
        except (OSError,ValueError,TypeError,KeyError):
            self.pane_order=["context","writing","notes"]
            self.pane_fractions={"context":1.0/3.0,"writing":1.0/3.0,"notes":1.0/3.0}
        self.legacy_recovery_file=self.legacy_state_dir/"recovery.json"
        self.legacy_position_file=self.legacy_state_dir/"positions.json"
        self.message="F1..F10 menus (Ctrl+1..Ctrl+0 where terminal supports modified keys; F1 FILE) · F12 notes (full-height on narrow screens) · Ctrl-S save · Ctrl-O open · Ctrl-D main menu · Ctrl-C quit · 3-pane on wide screens (>=150 cols)"
        self.bold=self.underline=self.show_spaces=False; self.align="LEFT"
        self.auto_capitalise=True
        self.left_margin=5; self.right_margin=5
        self.page_columns=72; self.page_preset="Standard"
        self.spell_dictionary=None
        # Three-pane workspace (context / writing / notes) state
        self.notes=[""]; self.ny=0; self.nx=0; self.notes_top=0; self.notes_dirty=False
        self.focus="editor"
        self.find_term=""; self.find_last_at=-1
        self.notes_file=self.state_dir/"notes.json"

    @staticmethod
    def default_documents_dir():
        """Where new documents save by default: ~/Documents when it exists,
        otherwise the current directory."""
        home_documents=Path.home()/"Documents"
        return home_documents if home_documents.is_dir() else Path.cwd()

    @staticmethod
    def next_default_document():
        number=1; base=WordProcessor.default_documents_dir()
        while True:
            candidate=base/f"Document {number}.rtf"
            if not candidate.exists():return candidate,number
            number+=1

    def default_save_name(self):
        """Pre-fill for the save prompt: the document's own name when it is
        not the generic numbered one, otherwise the next default name."""
        name=(self.name or "").strip()
        if name and name!=self.next_default_document()[0].stem:
            if name.lower().endswith((".rtf",".txt")):return name
            return name+".rtf"
        return self.next_default_document()[0].name

    def put(self,y,x,text,attr=0):
        h,w=self.s.getmaxyx()
        if 0<=y<h and x<w:
            try:self.s.addnstr(y,max(0,x),str(text),max(0,w-max(0,x)-1),attr)
            except curses.error:pass

    def draw(self):
        if self.wide_mode():
            self.draw_wide()
            return
        self.s.erase(); h,w=self.s.getmaxyx()
        if h<12 or w<45:
            self.put(0,0,"Please enlarge terminal to at least 45 x 12."); self.s.refresh(); return
        if self.focus=="notes":
            # Narrow terminal: no room for the three-pane workspace, so the
            # notes get their own full-height view (F12 toggles in and out).
            self.draw_narrow_notes(h,w)
            return
        # Highlight = black text on the orange/yellow bar. No A_BOLD: bold
        # black renders as grey on many terminal themes.
        hi=curses.color_pair(2)
        self.put(0,1," WordPro  /  TERMINAL WORD PROCESSOR ",hi)
        self.put(0,max(1,w-20),"● UNSAVED" if self.dirty else "● SAVED",curses.A_BOLD)
        self.put(1,1,self.name+(" *" if self.dirty else ""),curses.A_BOLD)
        self.put(2,1,"─"*(w-2)); stride=max(7,(w-4)//len(TABS))
        for i,t in enumerate(TABS):self.put(3,2+i*stride,t,hi if i==self.tab else curses.A_DIM)
        self.put(4,1,"─"*(w-2))
        page_width=max(1,min(self.page_columns,w-10)); page_x=max(0,(w-page_width)//2)
        left,right=self.effective_margins(page_width)
        self.render_ruler(5,page_x,page_width,left,right)
        cursor_pos=self.render_editor_content(page_x,page_width,6,indent=left,right_margin=right)
        self.put(h-3,1,"─"*(w-2))
        page_number=self.current_page_number(self.y)
        self.put(h-2,1,f"PAGE {page_number}   LINE {self.y+1}   COL {self.x+1}   WORDS {len(' '.join(self.lines).split())}   CHARS {sum(map(len,self.lines))}   TEXT {self.content_width()} COLS")
        self.put(h-1,1,self.message)
        self.move_cursor(cursor_pos)
        self.s.refresh()

    def draw_narrow_notes(self,h,w):
        """Narrow terminal (<150 cols): the notes pane has no room in the
        single-column layout, so when focused (F12) the notes get a full-height
        view of their own. F12 again saves the notes and returns to writing."""
        hi=curses.color_pair(2)
        self.put(0,1," WordPro  /  TERMINAL WORD PROCESSOR ",hi)
        self.put(0,max(1,w-20),"● UNSAVED" if self.dirty else "● SAVED",curses.A_BOLD)
        self.put(1,1,"NOTES",hi)
        self.put(1,8,self.name+(" *" if self.notes_dirty else ""),curses.A_BOLD)
        self.put(1,max(1,w-23),"[F12 back to writing]",curses.A_BOLD)
        self.put(2,1,"─"*(w-2)); stride=max(7,(w-4)//len(TABS))
        for i,t in enumerate(TABS):self.put(3,2+i*stride,t,hi if i==self.tab else curses.A_DIM)
        self.put(4,1,"─"*(w-2))
        y0=5
        rows=max(1,h-3-y0)
        notes_width=max(1,w-3)
        notes_visual=self.notes_visual_row(self.ny,self.nx,notes_width)
        if notes_visual<self.notes_top:self.notes_top=notes_visual
        elif notes_visual>=self.notes_top+rows:self.notes_top=notes_visual-rows+1
        notes_pos=self.render_pane_text(1,w-2,y0,rows,self.notes,
                                        scroll=self.notes_top,cursor=(self.ny,self.nx),wrap=True)
        self.put(h-3,1,"─"*(w-2))
        self.put(h-2,1,f"NOTES   LINE {self.ny+1}   COL {self.nx+1}   WORDS {len(' '.join(self.notes).split())}   CHARS {sum(map(len,self.notes))}   FOCUS NOTES")
        self.put(h-1,1,self.message)
        self.move_cursor(notes_pos)
        self.s.refresh()

    def render_editor_content(self,x0,width,y0,indent=0,right_margin=0,max_rows=None):
        """Render the editor's lines (page separators, alignment) into the
        region starting at (y0,x0) that is `width` columns wide. The text is
        inset by `indent` on the left and leaves `right_margin` free on the
        right. Returns the cursor's screen position (row, col) or None.
        `max_rows` caps the number of lines rendered (the wide layout passes
        its pane height, which is one less than the single-column editor's).
        The physical cursor is NOT moved here — the caller must place it
        after ALL text output (curses leaves the cursor where the last write
        happened), via move_cursor()."""
        h,w=self.s.getmaxyx()
        visible=max(1,h-9)
        if max_rows is not None:visible=max(1,min(visible,max_rows))
        text_width=max(1,width-indent-right_margin)
        items,line_to_di=self.display_items()
        if 0<=self.top<len(self.lines) and self.top in line_to_di:
            start_di=line_to_di[self.top]
            if start_di>0 and items[start_di-1][0]=="sep":start_di-=1
        else:
            start_di=0
        for row,di in enumerate(range(start_di,min(len(items),start_di+visible))):
            kind,n=items[di]
            if kind=="sep":
                self.put(y0+row,x0,"─"*width,curses.color_pair(1)); continue
            if kind=="blank":
                # Leave the rest of the page visually empty; only the page
                # boundary itself receives a horizontal rule.
                continue
            if kind=="break":
                marker="─ ␌ NEW PAGE ␌ "
                self.put(y0+row,x0,(marker+"─"*max(0,width-len(marker)))[:width],curses.A_BOLD|curses.A_DIM); continue
            raw=self.lines[n]
            line=raw.replace(" ","·") if self.show_spaces else raw
            if self.align=="CENTER":line=line.center(text_width)
            elif self.align=="RIGHT":line=line.rjust(text_width)
            elif self.align=="JUSTIFY":
                last_of_para=(n+1>=len(self.lines))or(self.lines[n+1].strip()=="")or(self.lines[n+1]=="\f")
                if not last_of_para:
                    line,_=self.justify_with_map(raw,text_width)
                    if self.show_spaces:line=line.replace(" ","·")
            attr=(curses.A_BOLD if self.bold or (n<len(self.line_formats) and self.line_formats[n].get("heading",0)) else 0)|(curses.A_UNDERLINE if self.underline else 0)
            self.put(y0+row,x0+indent,line[:text_width],attr)
        cursor_pos=None
        if self.y in line_to_di:
            cy=y0+(line_to_di[self.y]-start_di)
            col=self.x
            if self.align=="JUSTIFY":
                n=self.y
                last_of_para=(n+1>=len(self.lines))or(self.lines[n+1].strip()=="")or(self.lines[n+1]=="\f")
                if not last_of_para:
                    _,jmap=self.justify_with_map(self.lines[n],text_width)
                    if 0<=self.x<len(jmap) and jmap[self.x]>=0:col=jmap[self.x]
            # Clamp to the actual text viewport. A line may have been wrapped
            # using a wider layout before a terminal resize; never leave the
            # hardware cursor outside the writing pane while the next redraw
            # catches up.
            col=max(0,min(col,text_width-1))
            cx=max(x0,min(x0+width-1,x0+indent+col))
            if y0<=cy<h-3 and cx<w-1:
                cursor_pos=(cy,cx)
        return cursor_pos

    def move_cursor(self,pos):
        """Place the physical cursor at (row,col). MUST be the last action
        before refresh(): curses leaves the cursor wherever the final write
        or move happened, so any put()/addnstr() after this call would drag
        it away from the pane again."""
        if pos is None:return
        cy,cx=pos
        try:self.s.move(cy,cx)
        except curses.error:pass

    def draw_wide(self):
        """Three-pane workspace. Panes are drawn in self.pane_order (left to
        right) with widths from self.pane_fractions; "Adjust view" (FILE menu)
        changes both, using the rulers on row 6 as mouse handles."""
        self.s.erase(); h,w=self.s.getmaxyx()
        if h<12:
            self.put(0,0,"Please enlarge terminal to at least 12 rows tall."); self.s.refresh(); return
        # Highlight = black text on the orange/yellow bar. No A_BOLD: bold
        # black renders as grey on many terminal themes.
        hi=curses.color_pair(2)
        self.put(0,1," WordPro  /  TERMINAL WORD PROCESSOR ",hi)
        self.put(0,max(1,w-20),"● UNSAVED" if self.dirty else "● SAVED",curses.A_BOLD)
        self.put(1,1,self.name+(" *" if self.dirty else ""),curses.A_BOLD)
        self.put(2,1,"─"*(w-2)); stride=max(7,(w-4)//len(TABS))
        for i,t in enumerate(TABS):self.put(3,2+i*stride,t,hi if i==self.tab else curses.A_DIM)
        self.put(4,1,"─"*(w-2))
        bounds,y0,y1=self.pane_bounds()
        rows=max(1,y1-y0)
        # Headers, in pane order.
        for name in self.pane_order:
            x0,x1=bounds[name]
            if name=="context":
                self.put(5,x0,"CONTEXT",curses.A_DIM)
            elif name=="writing":
                self.put(5,x0,"WRITING",hi if self.focus=="editor" else curses.A_DIM)
            else:
                self.put(5,x0,"NOTES  [F12]",hi if self.focus=="notes" else curses.A_DIM)
        # Row 6: while adjusting the view every pane shows an interactive
        # ruler; otherwise the writing pane shows its margin ruler and the
        # other panes a plain separator.
        for name in self.pane_order:
            x0,x1=bounds[name]
            width=x1-x0+1
            if self.adjust_view:
                self._draw_active_ruler(6,x0,width,name)
            elif name=="writing":
                page_width=max(1,min(width,self.page_columns))
                w_left,w_right=self.effective_margins(page_width)
                center_offset=max(0,(width-page_width)//2)
                self.render_ruler(6,x0+center_offset,page_width,w_left,w_right)
            else:
                self.put(6,x0,"─"*width,curses.A_DIM)
        # Pane content, in pane order.
        editor_pos=None
        notes_pos=None
        for name in self.pane_order:
            x0,x1=bounds[name]
            width=x1-x0+1
            if name=="context":
                # Keep context text left-justified, but inset it so it does
                # not hug the far-left side of its column.
                context_inset=min(8,max(0,(x1-x0)//5))
                self.render_context(x0+context_inset,max(1,width-context_inset),y0,rows)
            elif name=="writing":
                page_width=max(1,min(width,self.page_columns))
                w_left,w_right=self.effective_margins(page_width)
                center_offset=max(0,(width-page_width)//2)
                center_right=max(0,width-center_offset-page_width)
                editor_pos=self.render_editor_content(
                    x0,width,y0,
                    indent=center_offset+w_left,
                    right_margin=center_right+w_right,
                    max_rows=rows)
            else:
                notes_pos=self.render_pane_text(x0,width,y0,rows,self.notes,
                                               scroll=self.notes_top,cursor=(self.ny,self.nx) if self.focus=="notes" else None,
                                               wrap=True)
        self.put(h-3,1,"─"*(w-2))
        page_number=self.current_page_number(self.y)
        self.put(h-2,1,f"PAGE {page_number}   LINE {self.y+1}   COL {self.x+1}   WORDS {len(' '.join(self.lines).split())}   CHARS {sum(map(len,self.lines))}   TEXT {self.content_width()} COLS   FOCUS {self.focus.upper()}")
        self.put(h-1,1,self.message)
        # Place the cursor LAST (after every write) so it stays in the active
        # pane: notes when focused there, otherwise the writing pane.
        self.move_cursor(notes_pos if (self.focus=="notes" and notes_pos) else editor_pos)
        self.s.refresh()

    def render_context(self,x0,width,y0,rows):
        """Show preceding document text, left-aligned and wrapped to the pane.

        Wrap at word boundaries rather than truncating each line, so narrowing
        the context pane never silently cuts off the ends of its text.
        """
        width=max(1,width)
        items,line_to_di=self.display_items()
        if 0 <= self.top < len(self.lines) and self.top in line_to_di:
            end_di=line_to_di[self.top]
            if end_di > 0 and items[end_di-1][0] == "sep":
                end_di -= 1
        else:
            end_di=0

        # Collect enough preceding logical lines to fill the visible area after
        # wrapping. Collect extra lines because a long paragraph may wrap.
        context=[]
        for kind,line_index in reversed(items[:end_di]):
            if kind == "text" and line_index is not None:
                source_line=self.lines[line_index]
                if not source_line:
                    context.append("")
                else:
                    # Word-wrap while preserving words; split only a single
                    # overlong word when it cannot fit on any row.
                    wrapped=[]
                    current=""
                    for word in source_line.split():
                        if not current:
                            if len(word) <= width:
                                current=word
                            else:
                                wrapped.extend(word[i:i+width] for i in range(0,len(word),width))
                        elif len(current)+1+len(word) <= width:
                            current += " " + word
                        else:
                            wrapped.append(current)
                            if len(word) <= width:
                                current=word
                            else:
                                wrapped.extend(word[i:i+width] for i in range(0,len(word),width))
                                current=""
                    if current:
                        wrapped.append(current)
                    context.extend(reversed(wrapped))
            elif kind == "break" and line_index is not None:
                context.append("")
            if len(context) >= rows+width:
                break

        context=list(reversed(context))[-rows:]
        offset=max(0,(rows-len(context))//2)
        for i,text in enumerate(context):
            r=y0+offset+i
            t=text.replace(" ","·") if self.show_spaces else text
            self.put(r,x0,t[:width],curses.A_DIM)

    def render_ruler(self,row,x0,width,left,right):
        """Draw the margin ruler for a writing area `width` columns wide,
        starting at column x0: a digit every 10 columns, a tick every 5, and
        'L'/'R' markers at the left and right margin positions — the same bar
        the single-column editor shows above its text."""
        ruler=["─"]*width
        for col in range(0,width,5):
            ruler[col]=str((col//10)%10) if col%10==0 else "┆"
        left_marker=min(left,width-1)
        right_marker=max(left_marker,min(width-right-1,width-1))
        ruler[left_marker]="L"
        if right_marker!=left_marker:ruler[right_marker]="R"
        self.put(row,x0,"".join(ruler),curses.A_DIM)

    def render_pane_text(self,x0,width,y0,rows,lines,scroll=0,cursor=None,wrap=False):
        """Render text in a pane. Optional soft wrapping keeps long notes visible
        without inserting hard line breaks into the saved note text. When wrapping,
        scroll is measured in displayed rows and the cursor is mapped from its
        logical line/column to the corresponding displayed row and column."""
        width=max(1,width)
        visual=[]
        if wrap:
            for logical_i,line in enumerate(lines):
                # Wrap on word boundaries where possible. Keep the starting
                # character index so the cursor maps correctly after soft wraps.
                pos=0
                while pos < len(line):
                    end=min(pos+width,len(line))
                    if end < len(line):
                        split=line.rfind(" ",pos,end+1)
                        if split > pos:
                            end=split
                    chunk=line[pos:end]
                    visual.append((logical_i,pos,chunk))
                    pos=end
                    while pos < len(line) and line[pos]==" ":
                        # Spaces at a wrap boundary belong to the preceding line
                        # visually, but remain in the underlying note unchanged.
                        pos += 1
                if not line:
                    visual.append((logical_i,0,""))
        else:
            visual=[(i,0,line) for i,line in enumerate(lines)]
        for row in range(rows):
            i=scroll+row
            if i>=len(visual):break
            _,_,text=visual[i]
            if self.show_spaces:text=text.replace(" ","·")
            self.put(y0+row,x0,text[:width],0)
        if cursor is not None:
            ci,cx=cursor
            if wrap:
                matches=[(j,item) for j,item in enumerate(visual) if item[0]==ci]
                chosen=matches[-1] if matches else (0,(ci,0,""))
                for j,item in matches:
                    _,start_col,text=item
                    if start_col <= cx <= start_col+len(text):
                        chosen=(j,item)
                        if cx < start_col+len(text) or j==matches[-1][0]:
                            break
                cy_index,(_,start_col,text)=chosen
                col=max(0,cx-start_col)
            else:
                cy_index=ci; col=cx
            cy=y0+(cy_index-scroll)
            if y0<=cy<y0+rows:
                return (cy,x0+min(max(0,col),width-1))
        return None

    def notes_visual_row(self,line_index,column,width):
        """Map a logical notes cursor to its soft-wrapped display row."""
        width=max(1,width)
        total=0
        for i,line in enumerate(self.notes):
            # Match render_pane_text's word-aware wrapping.
            pos=0
            starts=[]
            while pos < len(line):
                end=min(pos+width,len(line))
                if end < len(line):
                    split=line.rfind(" ",pos,end+1)
                    if split > pos:
                        end=split
                starts.append(pos)
                pos=end
                while pos < len(line) and line[pos]==" ":
                    pos += 1
            if not starts:
                starts=[0]
            chunks=len(starts)
            if i==line_index:
                row=max((j for j,start in enumerate(starts) if start<=column),default=0)
                return total+min(row,chunks-1)
            total+=chunks
        return max(0,total-1)

    def prompt(self,label,initial=""):
        self.s.timeout(-1); curses.noecho(); value=initial
        while True:
            h,w=self.s.getmaxyx(); self.put(h-1,1," "*(w-2))
            self.put(h-1,1,label+value,curses.color_pair(2))
            self.s.move(h-1,min(w-2,len(label+value))); self.s.refresh()
            try:key=self.s.get_wch()
            except KeyboardInterrupt:
                self.ctrl_c_pending=True
                if self.in_quit_prompt:self.ctrl_c_pending=False
                self.s.timeout(750); self.message="Cancelled."; return None
            except curses.error:continue
            if key in (27,"\x1b"):
                self.s.timeout(750); self.message="Cancelled."; return None
            if key in ("\n","\r",10,13):
                self.s.timeout(750); return value
            if key in (curses.KEY_BACKSPACE,"\x7f","\b"):
                value=value[:-1]
            elif isinstance(key,str) and key.isprintable():
                value+=key

    def prompt_password(self,label):
        """Like prompt(), but the typed value is masked with dots."""
        self.s.timeout(-1); curses.noecho(); value=""
        while True:
            h,w=self.s.getmaxyx(); self.put(h-1,1," "*(w-2))
            self.put(h-1,1,label+"•"*len(value),curses.color_pair(2))
            self.s.move(h-1,min(w-2,len(label)+len(value))); self.s.refresh()
            try:key=self.s.get_wch()
            except KeyboardInterrupt:
                self.ctrl_c_pending=True
                if self.in_quit_prompt:self.ctrl_c_pending=False
                self.s.timeout(750); self.message="Cancelled."; return None
            except curses.error:continue
            if key in (27,"\x1b"):
                self.s.timeout(750); self.message="Cancelled."; return None
            if key in ("\n","\r",10,13):
                self.s.timeout(750); return value
            if key in (curses.KEY_BACKSPACE,"\x7f","\b"):
                value=value[:-1]
            elif isinstance(key,str) and key.isprintable():
                value+=key

    def clear_recovery(self):
        for path in (self.recovery_file,self.legacy_recovery_file):
            try:path.unlink(missing_ok=True)
            except OSError:pass

    def document_positions(self):
        try:
            source=self.position_file if self.position_file.exists() else self.legacy_position_file
            value=json.loads(source.read_text(encoding="utf-8"))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError,TypeError):return {}

    @staticmethod
    def position_key(path):
        return str(Path(path).expanduser().resolve())

    def save_position(self,path=None):
        path=path or self.path
        if not path:return
        positions=self.document_positions()
        positions[self.position_key(path)]={"line":self.y,"column":self.x}
        try:
            self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.state_dir,delete=False) as f:
                json.dump(positions,f)
                temp=Path(f.name)
            os.replace(temp,self.position_file)
        except OSError as e:self.message=f"Could not remember cursor position: {e}"

    def restore_position(self,path):
        position=self.document_positions().get(self.position_key(path),{})
        try:
            self.y=max(0,min(int(position.get("line",0)),len(self.lines)-1))
            self.x=max(0,min(int(position.get("column",0)),len(self.lines[self.y])))
        except (AttributeError,TypeError,ValueError):self.y=self.x=0
        self.top=max(0,self.y-2)

    def notes_file_for(self,path):
        """The notes file for a document, saved next to it as '<name>.notes'
        (e.g. 'story.rtf' -> 'story.rtf.notes')."""
        return Path(str(path)+".notes")

    def _legacy_notes_entry(self,path):
        """Read a legacy notes entry from the state-dir notes.json, so notes
        saved before the '.notes' switch are migrated rather than lost."""
        try:
            value=json.loads(self.notes_file.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError):
            return None
        if isinstance(value,dict):
            entry=value.get(self.position_key(path),{})
            if isinstance(entry,dict) and isinstance(entry.get("lines"),list) and entry["lines"]:
                return entry
        return None

    def load_notes(self,path=None):
        """Load the per-document notes from '<document>.notes' next to the file."""
        path=path or self.path
        if not path:
            self.notes=[""]; self.ny=self.nx=self.notes_top=0; self.notes_dirty=False
            return
        notes_file=self.notes_file_for(path)
        text=None
        try:
            text=notes_file.read_text(encoding="utf-8")
        except (OSError,UnicodeDecodeError):
            entry=self._legacy_notes_entry(path)
            if entry is not None:
                text="\n".join(entry["lines"])
                try:
                    notes_file.parent.mkdir(parents=True,exist_ok=True)
                    with open(notes_file,"w",encoding="utf-8") as f:f.write(text)
                except OSError:pass
        if text is None:
            self.notes=[""]; self.ny=self.nx=self.notes_top=0; self.notes_dirty=False
            return
        self.notes=text.split("\n") if text else [""]
        self.ny=len(self.notes)-1
        self.nx=len(self.notes[self.ny])
        self.notes_top=0
        self.notes_dirty=False

    def save_notes(self,path=None):
        """Save the per-document notes to '<document>.notes' next to the file."""
        path=path or self.path
        if not path:return
        if not self.notes_dirty:return
        notes_file=self.notes_file_for(path)
        try:
            notes_file.parent.mkdir(parents=True,exist_ok=True)
            mode=notes_file.stat().st_mode & 0o777 if notes_file.exists() else None
            text="\n".join(self.notes)
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=notes_file.parent,prefix=".notes.",suffix=".tmp",delete=False) as f:
                f.write(text); temp=Path(f.name)
            if mode is not None:temp.chmod(mode)
            os.replace(temp,notes_file)
            self.notes_dirty=False
        except OSError as e:self.message=f"Could not save notes: {e}"

    def carry_notes(self,old_path,new_path):
        """Give a renamed/moved document its old notes. When a save lands under
        a new name (Save As) and the old name had a notes file but the new name
        has none yet, copy the notes over so they follow the document.
        Non-destructive (like Save As itself): the old notes file stays put.
        Returns True if notes were carried."""
        old_notes=self.notes_file_for(old_path)
        new_notes=self.notes_file_for(new_path)
        if old_notes==new_notes:return False
        if not old_notes.is_file() or new_notes.exists():return False
        try:
            new_notes.parent.mkdir(parents=True,exist_ok=True)
            mode=old_notes.stat().st_mode & 0o777
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=new_notes.parent,prefix=".notes.",suffix=".tmp",delete=False) as f:
                f.write(old_notes.read_text(encoding="utf-8")); temp=Path(f.name)
            temp.chmod(mode)
            os.replace(temp,new_notes)
            return True
        except OSError:
            try:temp.unlink(missing_ok=True)
            except (OSError,NameError):pass
            return False

    @staticmethod
    def backup_old(path):
        """Keep an existing document beside the file before replacing/discarding its draft."""
        path=Path(path)
        if not path.is_file():return None
        backup=path.with_name(path.name+".old")
        suffix=1
        while backup.exists():
            backup=path.with_name(path.name+f".old.{suffix}"); suffix+=1
        temp=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,prefix=f".{path.name}.",delete=False) as f:
                temp=Path(f.name)
            shutil.copy2(path,temp)
            os.replace(temp,backup)
            return backup
        except OSError:
            if temp is not None:
                try:temp.unlink(missing_ok=True)
                except OSError:pass
            raise

    def write_recovery(self,quiet=False):
        # quiet=True is for the background auto-save in the main loop: it must
        # not clobber a status message the user is reading (e.g. a Find result).
        try:
            self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            payload={"name":self.name,"path":str(self.path) if self.path else None,"lines":self.lines,
                     "line_formats":self.line_formats,
                     "y":self.y,"x":self.x,
                     "left_margin":self.left_margin,"right_margin":self.right_margin,"page_columns":self.page_columns,
                     "page_preset":self.page_preset,"bold":self.bold,"underline":self.underline,
                     "spell_dictionary":str(self.spell_dictionary) if self.spell_dictionary else None}
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.state_dir,delete=False) as f:
                json.dump(payload,f,ensure_ascii=False)
                temp=Path(f.name)
            os.replace(temp,self.recovery_file)
            if not quiet:self.message="Recovery draft saved."
            return True
        except OSError as e:
            if not quiet:self.message=f"Recovery draft failed: {e}"
            return False

    def recover_draft(self):
        source=self.recovery_file if self.recovery_file.exists() else self.legacy_recovery_file
        if not source.exists():return False
        answer=self.prompt("RECOVER PREVIOUS DRAFT? (Y/N) ")
        if answer is None:return False
        if answer.lower()!="y":
            try:
                draft=json.loads(source.read_text(encoding="utf-8"))
                original=Path(draft["path"]) if draft.get("path") else None
                backup=self.backup_old(original) if original else None
            except (OSError,ValueError,TypeError) as e:
                self.message=f"Recovery was kept because its original could not be backed up: {e}"
                return False
            self.clear_recovery()
            if backup:self.message=f"Ignored recovery draft; original backed up to {backup}"
            return False
        try:
            draft=json.loads(source.read_text(encoding="utf-8"))
            self.lines=draft.get("lines") or [""]
            self.line_formats=draft.get("line_formats") or [{"heading":0,"size":10} for _ in self.lines]
            self.sync_line_formats()
            self.name=draft.get("name","UNTITLED")
            self.path=Path(draft["path"]) if draft.get("path") else None
            self.left_margin=int(draft.get("left_margin",5)); self.right_margin=int(draft.get("right_margin",5))
            self.page_columns=int(draft.get("page_columns",72))
            self.page_preset=draft.get("page_preset","Standard")
            self.bold=bool(draft.get("bold",False)); self.underline=bool(draft.get("underline",False))
            self.spell_dictionary=Path(draft["spell_dictionary"]) if draft.get("spell_dictionary") else None
            self.wrap_document()
            self.y=max(0,min(int(draft.get("y",0)),len(self.lines)-1))
            self.x=max(0,min(int(draft.get("x",0)),len(self.lines[self.y])))
            self.top=max(0,self.y-2); self.dirty=True
            self.load_notes()
            self.recovered_draft=True
            self.message="Recovered draft. Save to keep your changes."
            return True
        except (OSError,ValueError,TypeError) as e:
            self.message=f"Could not recover draft: {e}"
            return False

    def save(self,target=None):
        target=target or self.path
        if not target:
            target=self.prompt(f"SAVE FILE AS (in {WordProcessor.default_documents_dir()}): ",self.default_save_name())
            if target is None or not target:self.message="Save cancelled."; return
        try:
            p=Path(target).expanduser()
            if not p.is_absolute() and p.parent==Path("."):
                p=WordProcessor.default_documents_dir()/p.name
            if p.suffix.lower()!=".rtf":p=p.with_suffix(".rtf") if p.suffix else p.with_name(p.name+".rtf")
            p.parent.mkdir(parents=True,exist_ok=True)
            mode=p.stat().st_mode & 0o777 if p.exists() else None
            if self.recovered_draft and self.path and p.resolve()==self.path.resolve():
                backup=self.backup_old(p)
                if backup:self.message=f"Original backed up to {backup}"
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=p.parent,prefix=f".{p.name}.",suffix=".tmp",delete=False) as f:
                f.write(self.to_rtf()); temp=Path(f.name)
            if mode is not None:temp.chmod(mode)
            os.replace(temp,p)
            old_path=self.path
            self.path=p; self.name=p.name; self.dirty=False
            self.recovered_draft=False
            self.clear_recovery()
            carried=False
            if old_path and old_path!=p:
                carried=self.carry_notes(old_path,p)
            self.save_notes()
            self.message=f"Saved {p}" + (" · notes copied to the new name" if carried else "")
        except OSError as e:self.message=f"Cannot save: {e}"

    def save_as(self):
        """FILE -> Save As: always ask for a new name, even when the document
        already has one (Save just re-writes the current file)."""
        target=self.prompt(f"SAVE AS (in {WordProcessor.default_documents_dir()}): ",self.default_save_name())
        if target is None or not target:self.message="Save cancelled."; return
        self.save(target)

    def open_file(self):
        if self.dirty:
            answer=self.prompt("DISCARD CURRENT DRAFT AND OPEN? (Y/N) ")
            if answer is None or answer.lower()!="y":return
        if self.path:self.save_position()
        target=self.prompt("OPEN FILE: ")
        if target is None or not target:return
        self.load_document(target)

    def load_document(self,target):
        try:
            path=Path(target).expanduser(); raw=path.read_bytes()
            try:source=raw.decode("utf-8-sig")
            except UnicodeDecodeError:source=raw.decode("cp1252")
            if source.lstrip().startswith("{\\rtf"):
                (self.lines,self.left_margin,self.right_margin,self.page_preset,self.page_columns,
                 self.bold,self.underline,self.line_formats)=self.from_rtf(source)
            else:
                self.lines=[]
                for paragraph in source.replace("\r\n","\n").replace("\r","\n").split("\n"):
                    parts=paragraph.split("\f"); self.lines.append(parts[0])
                    for part in parts[1:]:self.lines.extend(("\f",part))
                self.left_margin=self.right_margin=5; self.page_preset="Standard"; self.page_columns=72
                self.bold=self.underline=False
                self.line_formats=[{"heading":0,"size":10} for _ in self.lines]
            self.path=path; self.wrap_document()
            self.name=path.name; self.restore_position(path); self.load_notes(path); self.dirty=False; self.message=f"Opened {self.path}"
            self.clear_recovery()
            return True
        except (OSError,UnicodeError,ValueError) as e:self.message=f"Cannot open: {e}"; return False

    def loading_frame(self,roots,index,fraction):
        w=self.s.getmaxyx()[1]
        self.s.erase()
        bar_width=max(10,min(60,w-12))
        fill=int(round(bar_width*max(0.0,min(1.0,fraction))))
        bar="█"*fill+"░"*(bar_width-fill)
        self.put(0,1,"LOADING DOCUMENTS",curses.color_pair(2))
        self.put(2,2,"["+bar+"]",curses.color_pair(1))
        self.put(4,2,f"scanning {roots[index]}",curses.A_DIM)
        self.s.refresh()

    def start_new_document(self,name=None):
        """Start a fresh document, named after `name` when given (which
        then pre-fills the save prompt), else the next default number.
        Clears any queued picker keystrokes so typed filter text cannot
        leak into the document body."""
        self.path=None
        name=(name or "").strip()
        if "/" in name:name=name.rsplit("/",1)[-1].strip()
        self.name=name or self.next_default_document()[0].stem
        self.lines=[""]
        self.line_formats=[{"heading":0,"size":10}]
        self.y=self.x=self.top=0
        self.dirty=False
        # Reset the per-document notes pane (a fresh doc has no notes yet).
        self.notes=[""]; self.ny=self.nx=self.notes_top=0; self.notes_dirty=False
        self.focus="editor"
        try:curses.flushinp()
        except curses.error:pass
        self.s.timeout(750)

    def scan_documents(self,roots):
        """Scan the picker roots for openable documents. The home Documents
        root is searched recursively; other roots stay top-level."""
        def find_documents(directory,recursive):
            found=[]
            if recursive:
                for base,names,files in os.walk(directory):
                    names[:]=[name for name in names if not name.startswith(".")]
                    base_path=Path(base)
                    for name in files:
                        if name.startswith("."):continue
                        path=base_path/name
                        if path.is_file() and path.suffix.lower() in (".rtf",".txt"):
                            found.append(path)
            else:
                for path in directory.iterdir():
                    if path.is_file() and path.suffix.lower() in (".rtf",".txt") and not path.name.startswith("."):
                        found.append(path)
            return found
        try:
            home_resolved=(roots[1] if len(roots)>1 else None)
            home_resolved=home_resolved.resolve() if home_resolved else None
        except OSError:
            home_resolved=None
        seen=set(); documents=[]; error=""
        started=time.monotonic()
        for index,directory in enumerate(roots):
            self.loading_frame(roots,index,index/max(1,len(roots)))
            recursive=False
            if home_resolved is not None:
                try:recursive=directory.resolve()==home_resolved
                except OSError:pass
            try:
                found=find_documents(directory,recursive)
            except OSError as e:
                error=f"Cannot scan {directory}: {e}"
                continue
            for path in found:
                try:
                    key=path.resolve()
                except OSError:
                    key=path
                if key not in seen:
                    seen.add(key)
                    try:label=path.relative_to(directory).as_posix()
                    except ValueError:label=path.name
                    documents.append((path,label))
            self.loading_frame(roots,index,(index+1)/max(1,len(roots)))
            time.sleep(0.04)
        remaining=0.35-(time.monotonic()-started)
        if remaining>0:
            self.loading_frame(roots,len(roots)-1,1.0)
            time.sleep(remaining)
        return sorted(documents,key=lambda entry:entry[1].casefold()),error

    def file_action(self,filtered,selected,action):
        """Copy or Move the selected picker entry, prompting for a
        destination (default: the documents folder)."""
        if not filtered:
            self.message="Nothing selected to "+action+"."; return False
        source=filtered[selected][0]
        label=f"COPY '{source.name}' TO: " if action=="copy" else f"MOVE '{source.name}' TO: "
        target=self.prompt(label,str(WordProcessor.default_documents_dir()))
        if target is None:
            self.message="Cancelled."; return False
        try:
            dest=Path(target).expanduser()
            if dest.is_dir() or (not dest.exists() and not dest.suffix):
                dest=dest/source.name
            dest.parent.mkdir(parents=True,exist_ok=True)
            if action=="copy":shutil.copy2(source,dest)
            else:shutil.move(str(source),str(dest))
            self.message=("Copied " if action=="copy" else "Moved ")+f"{source.name} to {dest}"
            return True
        except (OSError,shutil.Error) as e:
            self.message=f"Cannot {action} file: {e}"
            return False

    @staticmethod
    def keyring_lookup(user,host):
        """Fetch a stored SMB password from the system keyring (Secret
        Service, e.g. KDE Wallet), if one exists."""
        try:
            result=subprocess.run(["secret-tool","lookup","user",user,"host",host],
                                  capture_output=True,text=True,timeout=15)
        except (OSError,subprocess.TimeoutExpired):
            return None
        if result.returncode==0 and result.stdout.strip():
            return result.stdout.rstrip("\n")
        return None

    @staticmethod
    def keyring_store(user,host,password):
        """Store an SMB password in the system keyring."""
        try:
            result=subprocess.run(["secret-tool","store","--label",f"WordPro SMB {user}@{host}",
                                  "user",user,"host",host],
                                  input=password,capture_output=True,text=True,timeout=20)
            return result.returncode==0
        except (OSError,subprocess.TimeoutExpired):
            return False

    @staticmethod
    def is_mounted(mountpoint):
        target=str(mountpoint)
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    fields=line.split()
                    if len(fields)>=2 and fields[1]==target:return True
        except OSError:pass
        return False

    def cifs_mount(self,host,share,mountpoint,user,password):
        """Mount //host/share at mountpoint. Returns an error string or None."""
        mountpoint.mkdir(parents=True,exist_ok=True)
        options=[f"uid={os.getuid()}",f"gid={os.getgid()}","iocharset=utf8","noprompt","vers=3.0"]
        if user:options.append(f"username={user}")
        cred=None
        if password is not None:
            try:
                self.state_dir.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.state_dir,
                                                 prefix=".cifs-cred-",delete=False) as cf:
                    if user:cf.write(f"username={user}\n")
                    cf.write(f"password={password}\n")
                    cred=Path(cf.name)
                cred.chmod(0o600)
                options.append(f"credentials={cred}")
            except OSError:pass
        cmd=["mount","-t","cifs",f"//{host}/{share}",str(mountpoint),"-o",",".join(options)]
        try:
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired) as e:
            return f"mount failed: {e}"
        finally:
            if cred is not None:
                try:cred.unlink(missing_ok=True)
                except OSError:pass
        if result.returncode!=0:
            detail=(result.stderr or result.stdout or "").strip()
            return f"Cannot mount //{host}/{share}" + (f": {detail}" if detail else "")
        return None

    def mount_share(self,url,allow_prompt=True):
        """Mount an smb:// or cifs:// share and return its local path, or
        (None, error). Passwords come from the keyring first, then a
        prompt (when allowed)."""
        parts=urlsplit(url)
        host=parts.hostname
        if not host:return None,"No host in URL."
        segments=[s for s in parts.path.split("/") if s]
        if not segments:return None,"No share in URL (smb://host/share/)."
        share=segments[0]; subpath=Path(*segments[1:]) if len(segments)>1 else Path(".")
        user=parts.username; password=parts.password
        mountpoint=self.state_dir/"mounts"/f"{host}_{share}"
        if self.is_mounted(mountpoint) and (mountpoint/subpath).is_dir():
            return mountpoint/subpath,None
        if password is None:
            password=self.keyring_lookup(user or "",host)
        if password is None and allow_prompt:
            if user is None:
                user=self.prompt("SMB USER: ")
                if user is None:return None,"Cancelled."
                user=user.strip() or None
            password=self.prompt_password(f"PASSWORD FOR {user or 'anonymous'}@{host}: ")
            if password is None:return None,"Cancelled."
        if password is None:
            return None,"No password (store it in the keyring or re-add the folder)."
        error=self.cifs_mount(host,share,mountpoint,user,password)
        if error:return None,error
        self.mounted_shares.append(mountpoint)
        if parts.password is None and self.keyring_lookup(user or "",host) is None:
            answer=self.prompt("STORE PASSWORD IN KEYRING? (Y/N) ")
            if answer is not None and answer.strip().lower()=="y":
                self.keyring_store(user or "",host,password)
        return mountpoint/subpath,None

    def resolve_folder_target(self,target,allow_prompt=True):
        """Resolve a folder spec (local path or smb:// URL) to an existing
        local directory, mounting network shares as needed."""
        target=target.strip()
        if target.lower().startswith(("smb://","cifs://")):
            return self.mount_share(target,allow_prompt=allow_prompt)
        path=Path(target).expanduser()
        if path.is_dir():
            return path,None
        return None,f"Folder not found: {path}"

    def unmount_all(self):
        for mountpoint in getattr(self,"mounted_shares",[]):
            try:
                subprocess.run(["umount",str(mountpoint)],capture_output=True,timeout=10)
            except (OSError,subprocess.TimeoutExpired):
                pass

    def load_added_folders(self):
        try:
            value=json.loads(self.added_folders_file.read_text(encoding="utf-8"))
            return [str(item) for item in value if isinstance(item,str)]
        except (OSError,ValueError):
            return []

    def save_added_folders(self,folders):
        try:
            self.state_dir.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.state_dir,delete=False) as f:
                json.dump([str(item) for item in folders],f)
                temp=Path(f.name)
            os.replace(temp,self.added_folders_file)
        except OSError:pass

    def document_picker(self):
        roots=[Path.cwd()]
        home_documents=Path.home()/"Documents"
        try:
            if home_documents.resolve()!=roots[0].resolve():roots.append(home_documents)
        except OSError:pass
        skipped=""
        for spec in self.load_added_folders():
            root,err=self.resolve_folder_target(spec,allow_prompt=False)
            if root is not None and root not in roots:
                roots.append(root)
            else:
                skipped=f"Skipped {spec}: {err or 'already listed.'}"
        documents,error=self.scan_documents(roots)
        if skipped:error=error or skipped
        query=""; selected=top=0; self.s.timeout(-1)
        menu_mode=False; menu_sel=0; menu_open=False; menu_item_sel=0
        while True:
            filtered=[entry for entry in documents if query.casefold() in entry[1].casefold()]
            selected=max(0,min(selected,len(filtered)-1))
            h,w=self.s.getmaxyx(); self.s.erase()
            self.put(0,1,"OPEN A DOCUMENT",curses.color_pair(2))
            self.put(1,1,f"Filter: {query}_   folders: {', '.join(str(root) for root in roots)}")
            bar_x=2
            for i,label in enumerate(("File","Add Folder","Refresh")):
                if menu_mode and menu_sel==i:self.put(2,bar_x,label,curses.color_pair(2))
                elif menu_mode:self.put(2,bar_x,label,0)
                else:self.put(2,bar_x,label,curses.A_DIM)
                bar_x+=len(label)+4
            self.put(3,1,"─"*max(1,w-2))
            if menu_open:
                for row,label in enumerate(("New","Copy","Move")):
                    self.put(4+row,4,label,curses.color_pair(2) if row==menu_item_sel else 0)
            else:
                visible=max(1,h-7)
                if selected<top:top=selected
                if selected>=top+visible:top=selected-visible+1
                if filtered:
                    for row,entry in enumerate(filtered[top:top+visible]):
                        self.put(4+row,2,entry[1],
                                 curses.color_pair(2) if top+row==selected else curses.A_NORMAL)
                elif query:
                    self.put(4,2,f"No match — Enter starts a new document named {query.strip().rsplit('/',1)[-1].strip()}.")
                else:
                    self.put(4,2,"No matching .rtf or .txt documents.")
            self.put(h-2,1,error or (self.message if self.message.startswith("Cannot open:") else ""))
            self.put(h-1,1,"Type to filter · Tab menus · ↑/↓ select · Enter open · Esc new document")
            self.s.refresh()
            try:key=self.s.get_wch()
            except KeyboardInterrupt:
                self.ctrl_c_pending=True; self.s.timeout(750); return False
            except curses.error:continue
            if menu_mode:
                if key in (27,"\x1b"):
                    if menu_open:menu_open=False
                    else:menu_mode=False
                    continue
                if key in ("\t",9):
                    menu_mode=False; menu_open=False
                    continue
                if not menu_open:
                    if key in (curses.KEY_LEFT,curses.KEY_RIGHT):
                        menu_sel=(menu_sel+(-1 if key==curses.KEY_LEFT else 1))%3
                        menu_item_sel=0
                    elif key in (10,13,"\n","\r",curses.KEY_ENTER,curses.KEY_DOWN):
                        if menu_sel==0:
                            menu_open=True; menu_item_sel=0
                        elif menu_sel==1:
                            target=self.prompt("ADD FOLDER PATH (local or smb://user@host/share/): ")
                            if target is not None and target.strip():
                                root,err=self.resolve_folder_target(target.strip())
                                if root is not None:
                                    if root not in roots:
                                        roots.append(root)
                                        self.save_added_folders(roots)
                                        documents,error=self.scan_documents(roots); selected=top=0
                                        error=f"Added {root}"
                                    else:
                                        error="Already in the list."
                                else:
                                    error=err
                            menu_mode=False; menu_open=False
                        else:
                            documents,error=self.scan_documents(roots)
                            selected=top=0; menu_mode=False
                    continue
                if key in (curses.KEY_UP,ord("k")):menu_item_sel=(menu_item_sel-1)%3; continue
                if key in (curses.KEY_DOWN,ord("j")):menu_item_sel=(menu_item_sel+1)%3; continue
                if key in (10,13,"\n","\r",curses.KEY_ENTER):
                    if menu_item_sel==0:
                        self.start_new_document(query.strip() or None)
                        return False
                    action="copy" if menu_item_sel==1 else "move"
                    done=self.file_action(filtered,selected,action)
                    if done:
                        documents,error=self.scan_documents(roots); selected=top=0
                    error=self.message
                    menu_mode=False; menu_open=False
                continue
            if key in ("\t",9):
                menu_mode=True; menu_sel=0; menu_open=False; menu_item_sel=0
                continue
            if key in (27,"\x1b"):
                # Escape starts a fresh document, named after the filter
                # text when one was typed (otherwise the default number).
                self.start_new_document(query.strip() or None)
                return False
            if key in (10,13,"\n","\r",curses.KEY_ENTER):
                if not filtered:
                    # No match: the typed filter becomes the name of a
                    # fresh document (and its pre-filled save name).
                    self.start_new_document(query.strip() or None)
                    return False
                if self.load_document(filtered[selected][0]):
                    self.s.timeout(750); return True
                error=self.message; continue
            if key in (curses.KEY_UP,):selected=max(0,selected-1)
            elif key in (curses.KEY_DOWN,):selected=min(max(0,len(filtered)-1),selected+1)
            elif key in (curses.KEY_BACKSPACE,127,"\x7f","\b"):
                query=query[:-1]; selected=top=0
            elif isinstance(key,str) and key.isprintable():
                query+=key; selected=top=0

    @staticmethod
    def rtf_escape(text):
        result=[]
        for char in text:
            if char in "\\{}":result.append("\\"+char)
            elif char=="\t":result.append(r"\tab ")
            elif ord(char)<128:result.append(char)
            else:
                units=char.encode("utf-16-le")
                for index in range(0,len(units),2):
                    value=int.from_bytes(units[index:index+2],"little")
                    if value>=32768:value-=65536
                    result.append(f"\\u{value}?")
        return "".join(result)

    @staticmethod
    def justify_with_map(line,width):
        """Justify a line to `width` columns (typographic justify) and return
        (justified, stored_to_display). stored_to_display[i] is the display
        column of stored character i, so the cursor can be placed correctly
        even though spaces are stretched. Full or single-word lines come back
        unchanged with an identity map."""
        def identity(s):return s,list(range(len(s)))
        line=line.rstrip()
        if len(line)>=width:return identity(line[:width])
        indent=len(line)-len(line.lstrip(" "))
        body=line.strip(" ")
        if not body:return identity(line)
        words=body.split()
        if len(words)<2:return identity(" "*indent+body)
        gaps=len(words)-1
        space=width-indent-sum(len(wd) for wd in words)
        if space<gaps:return identity(line)  # not enough room for a space per gap
        base,extra=divmod(space,gaps)
        stored_to_display=[-1]*len(line)
        dpos=0
        for i in range(indent):stored_to_display[i]=dpos; dpos+=1
        spos=indent
        for i,wd in enumerate(words):
            if i>0:
                stored_to_display[spos]=dpos; spos+=1
                dpos+=base+(1 if i<=extra else 0)
            for ch in wd:
                stored_to_display[spos]=dpos; spos+=1; dpos+=1
        display=[" "]*indent
        for i,wd in enumerate(words):
            if i>0:display.append(" "*(base+(1 if i<=extra else 0)))
            display.append(wd)
        return "".join(display),stored_to_display

    def to_rtf(self):
        papers={"A4":(11906,16838),"A5":(8391,11906),"Standard":(12240,15840),
                "Normal":(10080,15840),"Wide":(14400,15840)}
        paperw,paperh=papers.get(self.page_preset,papers["Standard"])
        self.sync_line_formats()
        paragraphs=[]; para_formats=[]; run=[]
        def flush_run():
            if not run:return
            body=["\\pard"]
            for idx,(line,fmt) in enumerate(run):
                size=max(6,min(72,int(fmt.get("size",10))))
                if idx>0:
                    prev=run[idx-1][0]
                    if not (prev.endswith((" ","\t")) or line.startswith((" ","\t"))):body.append(" ")
                body.append(f"\\b{int(self.bold)}\\ul{int(self.underline)}\\fs{size*2} ")
                body.append(self.rtf_escape(line))
            paragraphs.append("".join(body))
            para_formats.append({"heading":0,"size":max(6,min(72,int(run[0][1].get("size",10))))})
            run.clear()
        for line,fmt in zip(self.lines,self.line_formats):
            if line=="\f":
                flush_run(); paragraphs.append("\\page"); para_formats.append({"heading":0,"size":10}); continue
            if not line.strip():
                flush_run(); paragraphs.append("\\pard"); para_formats.append({"heading":0,"size":10}); continue
            heading=max(0,min(3,int(fmt.get("heading",0))))
            if heading:
                flush_run()
                size=max(6,min(72,int(fmt.get("size",10))))
                paragraphs.append(f"\\pard\\sb240\\sa120\\keepn\\outlinelevel{heading-1}\\b1\\ul{int(self.underline)}\\fs{size*2} {self.rtf_escape(line)}")
                para_formats.append({"heading":heading,"size":size}); continue
            run.append((line,fmt))
        flush_run()
        paragraphs="\\par\n".join(paragraphs)
        return ("{\\rtf1\\ansi\\deff0\\uc1\n"
                "{\\fonttbl{\\f0\\fnil Courier New;}}\n"
                f"{{\\*\\wordpro preset={self.page_preset} columns={self.page_columns} left={self.left_margin} right={self.right_margin} bold={int(self.bold)} underline={int(self.underline)}}}\n"
                "{\\*\\wpformats "+",".join(f"h{int(f.get('heading',0))}s{int(f.get('size',10))}" for f in para_formats)+"}\n"
                f"\\paperw{paperw}\\paperh{paperh}\\margl{self.left_margin*120}\\margr{self.right_margin*120}\\margt1440\\margb1440\n"
                f"\\viewkind4\\f0 {paragraphs}}}")

    @staticmethod
    def from_rtf(source):
        metadata=re.search(r"\\\*\\(?:wordpro|wp2400)\s+([^}]*)",source)
        values=dict(re.findall(r"(preset|columns|left|right|bold|underline)=([^\s]+)",metadata.group(1))) if metadata else {}
        format_metadata=re.search(r"\\\*\\wpformats\s+([^}\s]+)",source)
        formats=[{"heading":int(h),"size":int(s)} for h,s in re.findall(r"h(\d+)s(\d+)",format_metadata.group(1))] if format_metadata else []
        destinations={"fonttbl","colortbl","stylesheet","latentstyles","info","generator","rsidtbl","wgrffmtfilter","pict","object","header","footer","filetbl","listtable","listoverridetable","wp","wordpro","wp2400","wpformats"}
        output=[]; stack=[]; skip=False; i=0; unicode_fallback=1; fallback_left=0; bold=False; underline=False
        while i<len(source):
            char=source[i]
            if char=="{":
                stack.append(skip)
                destination=re.match(r"\{\s*\\(?:\*\s*\\)?([A-Za-z]+)",source[i:])
                skip=skip or bool(destination and destination.group(1) in destinations)
                i+=1; continue
            if char=="}":
                if stack:skip=stack.pop()
                i+=1; continue
            if char!="\\":
                if not skip:
                    if char in "\r\n":pass
                    elif fallback_left:fallback_left-=1
                    else:output.append(char)
                i+=1; continue
            i+=1
            if i>=len(source):break
            if source[i] in "\\{}":
                if not skip:
                    if fallback_left:fallback_left-=1
                    else:output.append(source[i])
                i+=1; continue
            if source[i]=="'" and i+2<len(source):
                try:decoded=bytes([int(source[i+1:i+3],16)]).decode("cp1252")
                except (ValueError,UnicodeDecodeError):decoded=""
                if not skip and not fallback_left:output.append(decoded)
                elif fallback_left:fallback_left-=1
                i+=3; continue
            if not source[i].isalpha():
                if not skip and source[i] in "~-":output.append(" " if source[i]=="~" else "-")
                i+=1; continue
            start=i
            while i<len(source) and source[i].isalpha():i+=1
            word=source[start:i]; number=None
            if i<len(source) and source[i] in "-0123456789":
                start=i
                if source[i]=="-":i+=1
                while i<len(source) and source[i].isdigit():i+=1
                try:number=int(source[start:i])
                except ValueError:number=None
            if i<len(source) and source[i]==" ":i+=1
            if skip:continue
            if word=="uc" and number is not None:unicode_fallback=max(0,number)
            elif word=="u" and number is not None:
                output.append(chr(number if number>=0 else number+65536)); fallback_left=unicode_fallback
            elif word=="page":output.append("\f")
            elif word=="pagebb":
                while output and output[-1]=="\n":output.pop()
                if output and output[-1]!="\f":output.extend(("\n","\f","\n"))
            elif word in ("par","line"):output.append("\n")
            elif word=="tab":output.append("\t")
            elif word=="b":bold=number!=0
            elif word in ("ul","ulw"):underline=number!=0
            elif word=="ulnone":underline=False
        text="".join(output).replace("\r","")
        text=text.encode("utf-16-le","surrogatepass").decode("utf-16-le","replace")
        lines=text.split("\n") or [""]
        preset=values.get("preset","Standard")
        columns=int(values.get("columns",72)); left=int(values.get("left",5)); right=int(values.get("right",5))
        if not values:
            paper=re.search(r"\\paperw(-?\d+)",source)
            margin_left=re.search(r"\\margl(-?\d+)",source); margin_right=re.search(r"\\margr(-?\d+)",source)
            width=int(paper.group(1)) if paper else 12240
            preset=min({"A4":11906,"A5":8391,"Standard":12240,"Normal":10080,"Wide":14400},key=lambda name:abs({"A4":11906,"A5":8391,"Standard":12240,"Normal":10080,"Wide":14400}[name]-width))
            left=round(int(margin_left.group(1))/120) if margin_left else 5
            right=round(int(margin_right.group(1))/120) if margin_right else 5
            columns={"A4":72,"A5":50,"Standard":72,"Normal":60,"Wide":100}[preset]
        left=max(0,left); right=max(0,right)
        limit=max(0,columns-MIN_TEXT_WIDTH)
        if left+right>limit:
            total=left+right; left=round(limit*left/total) if total else 0; right=limit-left
        if len(formats)<len(lines):formats.extend({"heading":0,"size":10} for _ in range(len(lines)-len(formats)))
        formats=formats[:len(lines)]
        return lines,left,right,preset,columns,bool(int(values.get("bold",bold))),bool(int(values.get("underline",underline))),formats

    def export_text(self,target):
        try:
            path=Path(target).expanduser()
            if not path.is_absolute() and path.parent==Path("."):
                path=WordProcessor.default_documents_dir()/path.name
            if path.suffix.lower()!=".txt":path=path.with_suffix(".txt") if path.suffix else path.with_name(path.name+".txt")
            path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=path.parent,prefix=f".{path.name}.",suffix=".tmp",delete=False) as f:
                f.write("\n".join(self.lines)); temp=Path(f.name)
            os.replace(temp,path); self.message=f"Exported plain text to {path}"
        except OSError as e:self.message=f"Cannot export text: {e}"

    def find_next_match(self):
        """Find the next case-insensitive occurrence, wrapping at document end."""
        if not self.find_term:
            term=self.prompt("FIND: ")
            if term is None or not term.strip():
                self.message="Find cancelled: enter a word or phrase."
                return
            self.find_term=term.strip()
            self.find_last_at=-1

        joined="\n".join(self.lines)
        folded=joined.lower()
        needle=self.find_term.lower()
        if not needle:
            self.message="Find cancelled: enter a word or phrase."
            return

        # Count all non-overlapping occurrences for the match indicator.
        matches=[]
        offset=0
        while True:
            at=folded.find(needle,offset)
            if at<0:break
            matches.append(at)
            offset=at+len(needle)

        if not matches:
            self.find_last_at=-1
            self.message=f"'{self.find_term}' not found (0 matches)."
            return

        if self.find_last_at >= 0:
            start=self.find_last_at+len(needle)
        else:
            start=sum(len(line)+1 for line in self.lines[:self.y])+self.x

        at=next((pos for pos in matches if pos>=start),matches[0])
        self.find_last_at=at
        self.y=joined[:at].count("\n")
        self.x=at-(joined.rfind("\n",0,at)+1)
        self.top=max(0,self.y-2)
        match_number=matches.index(at)+1
        wrapped=" (wrapped)" if at<start else ""
        self.message=f"Find '{self.find_term}': match {match_number} of {len(matches)}{wrapped}."

    def action(self,item):
        if item=="Main menu":
            self.go_to_main_menu(); return
        elif item=="Save":
            self.save(); return
        elif item=="Save As":
            self.save_as(); return
        elif item=="Adjust view":
            self.begin_adjust_view(); return
        elif item=="Quit":
            self.quit_flow(); return
        elif item=="Scroll top":self.y=0; self.x=min(self.x,len(self.lines[0])); self.top=0
        elif item=="Scroll bottom":self.y=len(self.lines)-1; self.x=min(self.x,len(self.lines[self.y]))
        elif item=="Normal":
            self.line_formats[self.y]["heading"]=0; self.dirty=True; self.message="Normal paragraph style applied."; return
        elif item.startswith("Heading "):
            level=int(item[-1]); self.line_formats[self.y]["heading"]=level
            self.line_formats[self.y]["size"]={1:20,2:16,3:14}[level]
            self.dirty=True; self.message=f"{item} style applied."; return
        elif item=="Font size":
            current=str(self.line_formats[self.y].get("size",10))
            value=self.prompt("FONT SIZE IN POINTS (6-72): ",current)
            if value is None:return
            try:size=int(value)
            except ValueError:self.message="Enter a whole point size from 6 to 72."; return
            if not 6<=size<=72:self.message="Font size must be from 6 to 72 points."; return
            self.line_formats[self.y]["size"]=size; self.dirty=True
            self.message=f"Font size set to {size} pt for this paragraph."; return
        elif item=="Bold":self.bold=not self.bold; self.dirty=True
        elif item=="Underline":self.underline=not self.underline; self.dirty=True
        elif item=="Show spaces":self.show_spaces=not self.show_spaces
        elif item in ("L margin", "R margin"):
            side = "left" if item == "L margin" else "right"
            attr = "left_margin" if side == "left" else "right_margin"
            page_width=max(MIN_TEXT_WIDTH+1,min(self.page_columns,self.s.getmaxyx()[1]-10))
            current=getattr(self,attr)
            value=self.prompt(f"{item.upper()} (0-{max(0,page_width-MIN_TEXT_WIDTH)} columns): ",str(current))
            if value is None:return
            try: candidate=int(value)
            except (TypeError,ValueError):
                self.message=f"{item} must be a whole number."; return
            other=self.right_margin if side=="left" else self.left_margin
            maximum=max(0,page_width-MIN_TEXT_WIDTH-other)
            if candidate<0 or candidate>maximum:
                self.message=f"{item} must be between 0 and {maximum} columns."; return
            setattr(self,attr,candidate)
            self.wrap_document(); self.dirty=True
            self.message=f"{item} set to {candidate} columns (text width {self.content_width()} cols)."; return
        elif item=="Margins":self.set_margins(); return
        elif item=="Import Hunspell dictionary":
            target=self.prompt("HUNSPELL .DIC FILE (matching .AFF required): ")
            if target is None:return
            dictionary=Path(target).expanduser()
            if dictionary.suffix.lower()!=".dic":dictionary=dictionary.with_suffix(".dic")
            affix=dictionary.with_suffix(".aff")
            if not dictionary.is_file() or not affix.is_file():
                self.message="Dictionary import needs matching .dic and .aff files."; return
            self.spell_dictionary=dictionary.with_suffix("")
            self.message=f"Hunspell dictionary selected: {dictionary.stem}."; return
        elif item=="Paper size":
            choices={"1":("A4",72),"2":("A5",50),"3":("Standard",72),
                     "4":("Normal",60),"5":("Wide",100)}
            current=next((key for key,(name,cols) in choices.items()
                          if name==self.page_preset and cols==self.page_columns),"3")
            choice=self.prompt("PAGE 1 A4  2 A5  3 Standard  4 Normal  5 Wide: ",current)
            if choice is None:return
            if choice not in choices:self.message="Choose a page setting from 1 to 5."; return
            self.page_preset,self.page_columns=choices[choice]; self.wrap_document(); self.dirty=True
            self.message=f"{self.page_preset} page setting selected ({self.page_columns} columns)."; return
        elif item in ("Insert header line","Insert footer line"):
            text=self.prompt("HEADER: " if item.startswith("Insert header") else "FOOTER: ")
            if text is None:return
            if item.startswith("Insert header"):
                self.lines.insert(0,text); self.line_formats.insert(0,{"heading":0,"size":10}); self.y+=1
            else:
                self.lines.append(text); self.line_formats.append({"heading":0,"size":10})
            self.wrap_document(); self.y=min(self.y,len(self.lines)-1); self.x=min(self.x,len(self.lines[self.y]))
            self.dirty=True; self.message=f"Inserted {item.removeprefix('Insert ').lower()}."; return
        elif item=="Insert page break":
            line=self.lines[self.y]; before=line[:self.x]; after=line[self.x:]
            self.lines[self.y:self.y+1]=[before,"\f",after]
            fmt=self.line_formats[self.y].copy(); self.line_formats[self.y:self.y+1]=[fmt,{"heading":0,"size":10},fmt.copy()]
            self.y+=2; self.x=0; self.dirty=True; self.wrap_document()
            self.y=min(self.y,len(self.lines)-1); self.x=min(self.x,len(self.lines[self.y]))
            self.message="Page break inserted."; return
        elif item in ("Center","Right align","Left align","Justify"):self.align={"Center":"CENTER","Right align":"RIGHT","Left align":"LEFT","Justify":"JUSTIFY"}[item]
        elif item=="Word count":
            self.message=f"{len(' '.join(self.lines).split())} words, {sum(map(len,self.lines))} characters."; return
        elif item=="Copy line":
            self.lines.insert(self.y+1,self.lines[self.y]); self.line_formats.insert(self.y+1,self.line_formats[self.y].copy())
            self.y+=1; self.dirty=True
        elif item=="Delete line":
            if len(self.lines)>1:
                self.lines.pop(self.y); self.line_formats.pop(self.y); self.y=min(self.y,len(self.lines)-1)
            else:self.lines=[""]; self.line_formats=[{"heading":0,"size":10}]; self.x=0
            self.dirty=True
        elif item in ("Move line up","Move line down"):
            target=self.y+(-1 if item.endswith("up") else 1)
            if 0<=target<len(self.lines):
                self.lines[self.y],self.lines[target]=self.lines[target],self.lines[self.y]
                self.line_formats[self.y],self.line_formats[target]=self.line_formats[target],self.line_formats[self.y]
                self.y=target; self.dirty=True
            self.message=f"{item} complete."; return
        elif item=="Find":
            term=self.prompt("FIND: ", self.find_term)
            if term is None:return
            term=term.strip()
            if not term:
                self.message="Find cancelled: enter a word or phrase."
                return
            self.find_term=term
            self.find_last_at=-1
            self.find_next_match()
            return
        elif item=="Replace":
            old=self.prompt("FIND: ")
            if old is None:return
            if old:
                new=self.prompt("REPLACE WITH: ")
                if new is None:return
                joined="\n".join(self.lines); count=joined.count(old)
                self.lines=joined.replace(old,new).split("\n")
                if count:
                    style=self.line_formats[min(self.y,len(self.line_formats)-1)].copy()
                    self.line_formats=[style.copy() for _ in self.lines]
                    self.wrap_document(); self.y=min(self.y,len(self.lines)-1); self.x=min(self.x,len(self.lines[self.y]))
                self.dirty|=bool(count); self.message=f"Replaced {count} occurrence(s)."
            return
        elif item=="Export plain text":
            target=self.prompt("EXPORT TEXT AS: ","document.txt")
            if target:self.export_text(target)
            return
        elif item=="Print preview":self.print_preview(); return
        elif item=="Spell check":self.proofread(spelling=True); return
        elif item=="Grammar check":self.proofread(spelling=False); return
        elif item=="Auto capitalisation":
            self.auto_capitalise=not self.auto_capitalise
            self.message="Automatic capitalisation is "+("ON." if self.auto_capitalise else "OFF.")
            return
        elif item=="Thesaurus":
            word=self.prompt("THESAURUS WORD: ")
            if word is None:return
            synonyms={"good":"great, fine, excellent, positive","bad":"poor, harmful, unpleasant",
                      "big":"large, huge, substantial","small":"little, tiny, compact",
                      "happy":"glad, cheerful, pleased","sad":"unhappy, sorrowful, downcast",
                      "fast":"quick, rapid, swift","important":"significant, major, meaningful"}
            found=synonyms.get(word.casefold())
            self.message=f"{word}: {found}" if found else f"No built-in suggestions for {word}."
            return
        elif item=="Page number":
            if self.lines[self.y]=="\f":
                if self.y+1<len(self.lines):self.y+=1
                else:self.lines.append(""); self.y+=1
                self.x=0
            page=self.current_page_number(self.y)
            marker=f"Page {page}"
            line=self.lines[self.y]; self.lines[self.y]=line[:self.x]+marker+line[self.x:]
            self.x+=len(marker); self.dirty=True; self.wrap_current_line(); self.message=f"Inserted {marker}."; return
        else:self.message=f"{item} selected."; return
        self.message=f"{item} toggled."

    def content_width(self):
        """Return the width available to document text in the active layout.

        In three-pane mode the centre writing pane, not the whole terminal or
        virtual page width, is the hard limit. This keeps wrapping and cursor
        placement inside the visible writing area on smaller monitors.
        """
        if self.wide_mode():
            bounds,_,_=self.pane_bounds()
            writing=bounds["writing"]
            pane_width=max(1, writing[1]-writing[0]+1)
            page_width=max(1,min(pane_width,self.page_columns))
            left,right=self.effective_margins(page_width)
            return max(1, page_width-left-right)
        terminal_width=self.s.getmaxyx()[1]
        page_width=max(1,min(self.page_columns,terminal_width-10))
        left,right=self.effective_margins(page_width)
        return max(MIN_TEXT_WIDTH,page_width-left-right)

    def wide_mode(self):
        """The three-pane workspace (context / writing / notes) is active when
        the terminal is wide enough to give each pane a usable width."""
        return self.s.getmaxyx()[1]>=WIDE_MIN_WIDTH

    def pane_bounds(self):
        """Return (bounds, y0, y1) for the wide three-pane layout, where
        bounds maps each pane name ("context","writing","notes") to its
        inclusive (x0,x1) column range.

        Pane order (left to right) is self.pane_order and each pane's width is
        its share (self.pane_fractions) of the usable width, so the panes can
        be rearranged and resized via "Adjust view".

        Layout (wide mode):
          row 5   pane headers (CONTEXT / WRITING / NOTES)
          row 6   separators, with the writing pane showing its margin ruler
          row 7.. the shared content area (y0 .. y1-1)"""
        H,W=self.s.getmaxyx()
        # Leave a more generous gutter at the left edge so the leftmost pane
        # sits further into the window instead of hugging the border.
        margin_left=10; margin_right=2; gap=1
        inner=max(3,W-margin_left-margin_right-2*gap)
        order=list(self.pane_order)
        total=sum(self.pane_fractions.get(n,1.0) for n in order) or 1.0
        # Proportional widths, at least 1 column each; the last pane absorbs
        # the rounding remainder so the panes always tile the usable width.
        widths=[]
        for n in order:
            widths.append(max(1,int(round(inner*self.pane_fractions.get(n,1.0)/total))))
        widths[-1]=max(1,widths[-1]+(inner-sum(widths)))
        bounds={}; x=margin_left
        for n,wdt in zip(order,widths):
            x1=min(W-1,x+wdt-1)
            bounds[n]=(x,x1)
            x=x1+gap+1
        return bounds,7,H-3

    def pane_at(self,x):
        """Name of the pane whose column range contains x (wide mode), else None."""
        bounds,_,_=self.pane_bounds()
        for n in self.pane_order:
            x0,x1=bounds[n]
            if x0<=x<=x1:return n
        return None

    def begin_adjust_view(self):
        """Enter view-adjust mode: interactive rulers on all three panes."""
        if not self.wide_mode():
            self.message="Adjust view needs the wide layout (terminal 150+ columns wide)."
            return
        self.layout_backup=(list(self.pane_order),dict(self.pane_fractions))
        self.view_drag=None
        self.adjust_view=True
        self.message="ADJUST VIEW: drag a ruler to resize that pane · click a ruler to move the pane right · Esc when done"

    def end_adjust_view(self):
        """Leave view-adjust mode and ask whether to save the new layout."""
        self.adjust_view=False
        self.view_drag=None
        answer=self.prompt("SAVE VIEW LAYOUT? (Y save / N revert) ")
        if answer is None:
            self.message="View kept for this session; not saved."
            return
        choice=answer.strip().lower()
        if choice=="y":
            self.save_layout()
        elif choice=="n":
            if self.layout_backup:
                self.pane_order,self.pane_fractions=self.layout_backup
            self.layout_backup=None
            self.message="View layout reverted; not saved."
        else:
            self.message="Choose Y to save the view, N to revert."

    def save_layout(self):
        try:
            self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            payload={"order":self.pane_order,"fractions":self.pane_fractions}
            with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.state_dir,delete=False) as f:
                json.dump(payload,f,ensure_ascii=False)
                temp=Path(f.name)
            os.replace(temp,self.layout_file)
            self.message="View layout saved."
        except OSError as e:
            self.message=f"Cannot save view layout: {e}"

    def _draw_active_ruler(self,row,x0,width,name):
        """Interactive ruler bar shown while adjusting the view."""
        label=f" ◄ {name.upper()} ► "
        ticks="┊"*(max(0,width-len(label)))
        self.put(row,x0,(label+ticks)[:width],curses.color_pair(2))

    def _apply_drag(self,x):
        """Resize the dragged pane so its edge follows x, borrowing from the
        adjacent pane (right neighbour, or the left one for the rightmost)."""
        name=self.view_drag["pane"]
        bounds,_,_=self.pane_bounds()
        order=self.pane_order
        i=order.index(name)
        if i+1<len(order):
            other=order[i+1]; side="right"
        else:
            other=order[i-1]; side="left"
        nx0,nx1=bounds[name]; ox0,ox1=bounds[other]
        min_w=MIN_TEXT_WIDTH
        pair=(nx1-nx0+1)+(ox1-ox0+1)
        if side=="right":
            new_w=x-nx0+1
        else:
            new_w=nx1-x+1
        new_w=max(min_w,min(pair-min_w,new_w))
        inner=sum(b[1]-b[0]+1 for b in bounds.values())
        self.pane_fractions=dict(self.pane_fractions)
        self.pane_fractions[name]=new_w/inner
        self.pane_fractions[other]=(pair-new_w)/inner

    def _finish_drag(self):
        drag=self.view_drag
        self.view_drag=None
        if not drag:return
        if drag["moved"]<=3:
            # A click (no real movement): move the pane one step right,
            # wrapping to the left, so repeated clicks cycle all positions.
            count=len(self.pane_order)
            i=self.pane_order.index(drag["pane"])
            name=self.pane_order.pop(i)
            self.pane_order.insert((i+1)%count,name)
            pos=self.pane_order.index(name)+1
            self.message=(f"Moved {name.upper()} to position {pos} of {len(self.pane_order)} · "
                          "drag a ruler to resize · Esc when done")
        else:
            self.message=(f"Resized {drag['pane'].upper()} · "
                          "drag a ruler to resize · click to move a pane · Esc when done")

    def _adjust_mouse(self,x,y,b):
        """Mouse handling while adjusting the view: the rulers (row 6) are
        drag handles (resize) and click targets (move the pane).

        ncurses builds differ in how they flag held-button motion (some use
        BUTTON1_MOVED, some re-report BUTTON1_PRESSED at the new position,
        and this one reports REPORT_MOUSE_POSITION), so while a drag is in
        progress every non-release event is treated as a motion report."""
        PRESSED=getattr(curses,"BUTTON1_PRESSED",0)
        RELEASED=getattr(curses,"BUTTON1_RELEASED",0)
        if self.view_drag is not None:
            if b & RELEASED:
                self._finish_drag()
            else:
                self._apply_drag(x)
                self.view_drag["moved"]=max(self.view_drag["moved"],abs(x-self.view_drag["start_x"]))
            return
        # A fresh press over a ruler starts a drag on that pane.
        if b & PRESSED and y==6:
            name=self.pane_at(x)
            if name:
                self.view_drag={"pane":name,"start_x":x,"moved":0}
                self.message=f"ADJUST VIEW: dragging {name.upper()} · release to keep · small move = move pane · Esc done"

    def effective_margins(self,page_width):
        left=max(0,self.left_margin); right=max(0,self.right_margin)
        limit=max(0,page_width-MIN_TEXT_WIDTH)
        if left+right>limit:
            total=left+right
            left=round(limit*left/total) if total else 0
            right=limit-left
        return left,right

    def page_line_capacity(self):
        return 37 if self.page_preset=="A5" else 58

    def current_page_number(self,line_index):
        page=1; used=0; capacity=self.page_line_capacity()
        for line in self.lines[:line_index+1]:
            if line=="\f":page+=1; used=0
            else:
                if used>=capacity:page+=1; used=0
                used+=1
        return page

    def document_pages(self):
        pages=[[]]; used=0; capacity=self.page_line_capacity()
        for line in self.lines:
            if line=="\f":pages.append([]); used=0
            else:
                if used>=capacity:pages.append([]); used=0
                pages[-1].append(line); used+=1
        return pages

    def display_items(self):
        """Editor render model: a list of (kind, line_index) items where kind
        is 'text' (content line), 'break' (an explicit \f page break) or
        'sep' (an automatic page boundary, drawn as a divider). Also returns
        a line_index -> item_index map used for cursor placement."""
        items=[]; line_to_di={}; used=0; capacity=self.page_line_capacity()
        for n,line in enumerate(self.lines):
            if line=="\f":
                # A page break is a single display row. Do not pad the rest of
                # the terminal viewport with blank rows: doing so means the
                # following page cannot render until the user scrolls through
                # an entire page of empty rows, and the preceding page appears
                # to vanish when scrolling back across the boundary.
                line_to_di[n]=len(items); items.append(("break",n))
                used=0
            else:
                if used>=capacity:
                    # Keep one visible boundary row between pages while keeping
                    # the display list compact enough that adjacent pages are
                    # rendered together as the user scrolls.
                    items.append(("sep",None))
                    used=0
                line_to_di[n]=len(items); items.append(("text",n)); used+=1
        return items,line_to_di

    def anchor_view(self):
        """Set self.top so the cursor stays in the MIDDLE of the writing
        area while typing, with the lines above and below it in view. A page
        separator approaching from below (or just crossed above) stays
        visible in the lower/upper half and scrolls past the cursor as new
        lines are typed in."""
        h,w=self.s.getmaxyx()
        # Use the same visible height the layout actually draws the writing
        # area with: the three-pane writing pane (rows 7..H-4) is one row
        # shorter than the single-column editor (rows 6..H-4).
        visible=max(1,h-10) if w>=WIDE_MIN_WIDTH else max(1,h-9)
        items,line_to_di=self.display_items()
        cursor_item=line_to_di.get(self.y, self.y)
        # Anchor the cursor on the middle row of the writing area (clamped at
        # the top of the document). Roughly half a screen of text stays
        # visible below the cursor, so nothing below it is ever cut off.
        start_di=max(0, cursor_item-visible//2)
        # self.top is a line index; use the first line at (or after) the
        # wanted display item.
        top_line=None
        for di2 in range(start_di, len(items)):
            if items[di2][1] is not None:
                top_line=items[di2][1]; break
        self.top=max(0, top_line if top_line is not None else 0)

    def hunspell_dictionary(self):
        if self.spell_dictionary:return str(self.spell_dictionary)
        bundled=Path(__file__).resolve().parent/"dictionaries"/"en_US"
        if bundled.with_suffix(".dic").is_file() and bundled.with_suffix(".aff").is_file():
            return str(bundled) if shutil.which("hunspell") else None
        if not shutil.which("hunspell"):return None
        try:
            result=subprocess.run(["hunspell","-D"],capture_output=True,text=True,timeout=3)
        except (OSError,subprocess.TimeoutExpired):return None
        section=result.stdout+result.stderr
        section=section.split("AVAILABLE DICTIONARIES",1)[-1].split("LOADED DICTIONARY",1)[0]
        dictionaries=[]
        for line in section.splitlines():
            value=line.strip().split()[0] if line.strip() else ""
            if value.startswith("/"):
                name=Path(value).name
                if name and name not in dictionaries:dictionaries.append(name)
        return next((name for name in dictionaries if name.lower().startswith("en_")),
                    dictionaries[0] if dictionaries else None)

    def language_tool_finding(self):
        text="\n".join(self.lines)
        data=urlencode({"text":text,"language":"en-GB"}).encode("utf-8")
        try:
            endpoint=os.environ.get("WORDPRO_LANGUAGETOOL_URL","http://127.0.0.1:8081").rstrip("/")
            with urlopen(endpoint+"/v2/check",data=data,timeout=4) as response:
                matches=json.loads(response.read().decode("utf-8")).get("matches",[])
        except (OSError,TimeoutError,ValueError):return None,None
        if not matches:return "LanguageTool",None
        match=matches[0]; offset=max(0,int(match.get("offset",0)))
        before=text[:offset]; row=before.count("\n"); col=len(before.rsplit("\n",1)[-1])
        message=match.get("message","Possible grammar issue")
        replacements=[item.get("value") for item in match.get("replacements",[])[:3] if item.get("value")]
        if replacements:message+=f"; suggestions: {', '.join(replacements)}"
        return "LanguageTool",(row,col,message)

    def proofread(self,spelling):
        common_typos={"teh":"the","adn":"and","recieve":"receive","seperate":"separate",
                      "definately":"definitely","occured":"occurred","untill":"until"}
        finding=None; engine="basic local"
        if spelling:
            dictionary=self.hunspell_dictionary()
            if dictionary:
                try:
                    result=subprocess.run(["hunspell","-d",dictionary,"-l"],input="\n".join(self.lines),
                                          capture_output=True,text=True,timeout=8)
                    if result.returncode==0 and "Can't open affix" not in result.stderr:
                        misspelled={word.strip().casefold() for word in result.stdout.splitlines() if word.strip()}
                        for row,line in enumerate(self.lines):
                            for word in re.finditer(r"[^\W\d_]+(?:['’][^\W\d_]+)*",line,re.UNICODE):
                                if word.group().casefold() in misspelled:
                                    finding=(row,word.start(),f"{word.group()} may be misspelled (Hunspell dictionary {dictionary})")
                                    break
                            if finding:break
                        engine="Hunspell"
                except (OSError,subprocess.TimeoutExpired):pass
            if engine=="basic local":
                for row,line in enumerate(self.lines):
                    for word in line.split():
                        clean=word.strip(".,!?;:()[]{}\"'“”‘’").lower()
                        if clean in common_typos:
                            finding=(row,line.find(word),f"{word} may be misspelled; try {common_typos[clean]}")
                            break
                    if finding:break
            label=f"Spell check ({engine})"
        else:
            engine,finding=self.language_tool_finding()
            if engine:
                label="Grammar check (LanguageTool)"
            else:
                engine="basic local"
                label="Grammar check (basic local; LanguageTool offline)"
            if engine=="LanguageTool":
                if finding:
                    self.y,self.x,note=finding; self.top=max(0,self.y-2)
                    self.message=f"{label}: line {self.y+1}, col {self.x+1}: {note}."
                else:self.message=f"{label}: no issues found."
                return
            for row,line in enumerate(self.lines):
                rules=[
                    (re.search(r"\b(\w+)\s+\1\b",line,re.IGNORECASE),"repeated word"),
                    (re.search(r"\s+[,.!?;:]",line),"space before punctuation"),
                    (re.search(r"(?:^|[.!?]\s+)([a-z])",line),"check sentence capitalization"),
                    (re.search(r"\bi\b",line),"capitalize the pronoun ‘I’"),
                    (re.search(r"\b(?:could|would|should|must) of\b",line,re.IGNORECASE),"use ‘have’ after this verb"),
                    (re.search(r"\b(alot)\b",line,re.IGNORECASE),"write ‘a lot’ as two words"),
                    (re.search(r"\b(your) welcome\b",line,re.IGNORECASE),"did you mean ‘you’re welcome’?"),
                    (re.search(r"\b(their) (?:is|are|was|were)\b",line,re.IGNORECASE),"check ‘their’ versus ‘there’"),
                ]
                match,advice=next(((m,advice) for m,advice in rules if m), (None,None))
                if match:
                    finding=(row,match.start(),advice); break
        if finding:
            self.y,self.x,note=finding; self.top=max(0,self.y-2); self.message=f"{label}: line {self.y+1}, col {self.x+1}: {note}."
        else:self.message=f"{label}: no basic issues found."

    def print_preview(self):
        self.s.timeout(-1); page=0; offset=0; pages=self.document_pages()
        while True:
            self.s.erase(); h,w=self.s.getmaxyx(); page=min(page,len(pages)-1)
            page_lines=pages[page]; view_height=max(1,h-6)
            offset=min(offset,max(0,len(page_lines)-view_height))
            self.put(0,1,f"PRINT PREVIEW  |  {self.name}",curses.A_BOLD)
            self.put(1,1,"─"*max(1,w-2))
            for row,line in enumerate(page_lines[offset:offset+view_height]):self.put(2+row,3,line)
            self.put(h-2,1,f"Page {page+1}/{len(pages)}  ·  Left/Right pages  Up/Down scroll  ·  Esc return")
            self.s.refresh()
            try:key=self.s.get_wch()
            except KeyboardInterrupt:
                self.ctrl_c_pending=True; self.s.timeout(750); return
            except curses.error:continue
            if key in (27,"\x1b"):self.s.timeout(750); return
            if key==curses.KEY_RIGHT:page=min(len(pages)-1,page+1); offset=0
            elif key==curses.KEY_LEFT:page=max(0,page-1); offset=0
            elif key==curses.KEY_DOWN:offset=min(max(0,len(page_lines)-view_height),offset+1)
            elif key==curses.KEY_UP:offset=max(0,offset-1)

    def wrap_document(self):
        self.sync_line_formats(); width=self.content_width(); wrapped=[]; wrapped_formats=[]
        for line,fmt in zip(self.lines,self.line_formats):
            if line=="\f":
                wrapped.append(line); wrapped_formats.append(fmt.copy()); continue
            while len(line)>width:
                cut=line.rfind(" ",0,width+1)
                if cut<=0:cut=width; skip=0
                else:skip=1
                wrapped.append(line[:cut])
                wrapped_formats.append(fmt.copy())
                line=line[cut+skip:].lstrip(" ")
            wrapped.append(line)
            wrapped_formats.append(fmt.copy())
        self.lines=wrapped or [""]
        self.line_formats=wrapped_formats or [{"heading":0,"size":10}]

    def sync_line_formats(self):
        if not hasattr(self,"line_formats"):self.line_formats=[]
        self.line_formats=self.line_formats[:len(self.lines)]
        while len(self.line_formats)<len(self.lines):self.line_formats.append({"heading":0,"size":10})
        for fmt in self.line_formats:
            fmt["heading"]=max(0,min(3,int(fmt.get("heading",0))))
            fmt["size"]=max(6,min(72,int(fmt.get("size",10))))

    def wrap_current_line(self):
        """Reflow the current paragraph after an edit, in both directions.

        The document stores visual lines rather than separate logical
        paragraphs.  Join the neighbouring wrapped lines into one paragraph,
        wrap it to the current width, and map the cursor through the reflow.
        Empty lines and form-feed page breaks remain hard boundaries.
        """
        width = max(1, self.content_width())
        if not self.lines:
            self.lines = [""]
            self.line_formats = [{"heading": 0, "size": 10}]
            self.y = self.x = 0
            return

        self.sync_line_formats()
        self.y = max(0, min(self.y, len(self.lines) - 1))
        self.x = max(0, min(self.x, len(self.lines[self.y])))
        if self.lines[self.y] == "\f":
            return

        # Find the paragraph boundaries. Blank lines and page breaks are
        # intentional hard breaks; ordinary wrapped lines are reflowable.
        start = self.y
        while start > 0 and self.lines[start - 1].strip() and self.lines[start - 1] != "\f":
            start -= 1
        end = self.y
        while end + 1 < len(self.lines) and self.lines[end + 1].strip() and self.lines[end + 1] != "\f":
            end += 1

        # Join visual lines into a logical paragraph. Wrapped lines normally
        # have no leading/trailing padding, but the active line may end in a
        # space that the user has just typed. Preserve that trailing space or
        # the editor makes it impossible to type word separators.
        parts = [line.strip() for line in self.lines[start:end + 1]]
        active_index = self.y - start
        active_raw = self.lines[self.y]
        active_leading = len(active_raw) - len(active_raw.lstrip())
        parts[active_index] = active_raw.lstrip()  # retain trailing spaces
        paragraph = " ".join(parts)

        # Map the cursor into the joined paragraph using the normalized line
        # lengths, accounting for the leading whitespace removed above.
        cursor_offset = max(0, self.x - active_leading)
        # `parts` is indexed relative to `start`, not by document line number.
        # Using range(start, self.y) raises IndexError once the paragraph is
        # below the first document line. Walk only the preceding paragraph parts.
        for idx in range(active_index):
            cursor_offset += len(parts[idx]) + 1
        cursor_offset = max(0, min(cursor_offset, len(paragraph)))
        fmt = self.line_formats[start].copy()

        wrapped = []
        offsets = []
        remaining = paragraph
        consumed_total = 0
        while len(remaining) > width:
            cut = remaining.rfind(" ", 0, width + 1)
            if cut <= 0:
                cut = width
                skip = 0
            else:
                skip = 1
            wrapped.append(remaining[:cut])
            consumed = cut + skip
            offsets.append(consumed_total)
            remaining = remaining[consumed:]
            consumed_total += consumed
        wrapped.append(remaining)
        offsets.append(consumed_total)

        # Find which newly wrapped line contains the cursor offset.
        new_y = start
        new_x = 0
        for n, line in enumerate(wrapped):
            line_start = offsets[n]
            line_end = line_start + len(line)
            if cursor_offset <= line_end or n == len(wrapped) - 1:
                new_y = start + n
                new_x = max(0, min(len(line), cursor_offset - line_start))
                break

        new_formats = [fmt.copy() for _ in wrapped]
        self.lines[start:end + 1] = wrapped
        self.line_formats[start:end + 1] = new_formats
        self.y = min(new_y, len(self.lines) - 1)
        self.x = min(new_x, len(self.lines[self.y]))

    def set_margins(self):
        old=(self.left_margin,self.right_margin); selected="L"; self.s.timeout(-1)
        page_width=max(MIN_TEXT_WIDTH+1,min(self.page_columns,self.s.getmaxyx()[1]-10))
        self.left_margin,self.right_margin=self.effective_margins(page_width)
        while True:
            # Show the live margins AND the resulting text width, so a setting
            # that would shrink the writing area is visible before applying.
            text_width=max(0,page_width-self.left_margin-self.right_margin)
            self.message=(f"MARGINS  L={self.left_margin}  R={self.right_margin}  ->  text {text_width} cols  "
                          f"({selected} selected · arrows adjust · Tab switch · Enter apply · Esc cancel)")
            self.draw()
            try:key=self.s.get_wch()
            except KeyboardInterrupt:
                self.ctrl_c_pending=True
                self.left_margin,self.right_margin=old
                self.s.timeout(750); return
            except curses.error:continue
            if key in (27,"\x1b"):
                self.left_margin,self.right_margin=old
                self.message="Margin change cancelled."; self.s.timeout(750); return
            if key in ("\t",9):selected="R" if selected=="L" else "L"
            elif key in ("l","L"):selected="L"
            elif key in ("r","R"):selected="R"
            elif key in (10,13,"\n","\r",curses.KEY_ENTER):
                self.wrap_document(); self.dirty=True
                self.message=(f"Margins set: left {self.left_margin}, right {self.right_margin} "
                              f"(text width {self.content_width()} cols).")
                self.s.timeout(750); return
            elif key in (curses.KEY_LEFT,curses.KEY_RIGHT):
                direction=-1 if key==curses.KEY_LEFT else 1
                page_width=max(MIN_TEXT_WIDTH+1,min(self.page_columns,self.s.getmaxyx()[1]-10))
                if selected=="L":
                    candidate=self.left_margin+direction
                    if candidate>=0 and candidate+self.right_margin<=page_width-MIN_TEXT_WIDTH:self.left_margin=candidate
                else:
                    candidate=self.right_margin-direction
                    if candidate>=0 and self.left_margin+candidate<=page_width-MIN_TEXT_WIDTH:self.right_margin=candidate

    @staticmethod
    def modified_key_code(sequence):
        try:
            if sequence.startswith("[27;5;") and sequence.endswith("~"):
                return int(sequence[6:-1])
            if sequence.startswith("[") and sequence.endswith("u"):
                parts=sequence[1:-1].split(";")
                if len(parts)==2 and int(parts[1])==5:return int(parts[0])
        except ValueError:
            pass
        return None

    def handle_modified_key(self,code):
        if code is None:return
        if 49<=code<=57:
            self.tab=code-49
            self.menu(self.tab)
        elif code==48:  # Ctrl-0 -> the 10th menu (BLOCK)
            self.tab=9
            self.menu(self.tab)
        elif code==115:self.save()
        elif code==111:self.open_file()
        elif code==13:self.action("Insert page break")
        elif code==100:self.go_to_main_menu()

    def go_to_main_menu(self):
        """Ctrl-D: return to the OPEN A DOCUMENT main menu, guarding any
        unsaved changes first (Esc stays in the current document)."""
        if self.dirty:
            answer=self.prompt("SAVE CHANGES BEFORE LEAVING? (Y/N, Esc stay) ")
            if answer is None:
                return
            if answer.strip().lower()=="y":
                self.save()
                if self.dirty:
                    return
        if self.path:self.save_position()
        self.document_picker()

    def notes_key(self,k):
        """Handle editing keys for the notes pane (routed in when focus=='notes').
        Operates on self.notes / self.ny / self.nx, independent of the document."""
        if isinstance(k,str):
            if k in ("\n","\r"):
                rest=self.notes[self.ny][self.nx:]
                self.notes[self.ny]=self.notes[self.ny][:self.nx]
                self.notes.insert(self.ny+1,rest); self.ny+=1; self.nx=0
            elif k in ("\x7f","\b"):
                if self.nx:self.notes[self.ny]=self.notes[self.ny][:self.nx-1]+self.notes[self.ny][self.nx:]; self.nx-=1
                elif self.ny:self.nx=len(self.notes[self.ny-1]); self.notes[self.ny-1]+=self.notes.pop(self.ny); self.ny-=1
            elif k=="\t":
                self.notes[self.ny]=self.notes[self.ny][:self.nx]+"    "+self.notes[self.ny][self.nx:]; self.nx+=4
            elif k.isprintable():
                self.notes[self.ny]=self.notes[self.ny][:self.nx]+k+self.notes[self.ny][self.nx:]; self.nx+=len(k)
        else:
            if k==curses.KEY_LEFT:
                if self.nx>0:self.nx-=1
                elif self.ny>0:self.ny-=1; self.nx=len(self.notes[self.ny])
            elif k==curses.KEY_RIGHT:
                if self.nx<len(self.notes[self.ny]):self.nx+=1
                elif self.ny<len(self.notes)-1:self.ny+=1; self.nx=0
            elif k==curses.KEY_UP:self.ny=max(0,self.ny-1); self.nx=min(self.nx,len(self.notes[self.ny]))
            elif k==curses.KEY_DOWN:self.ny=min(len(self.notes)-1,self.ny+1); self.nx=min(self.nx,len(self.notes[self.ny]))
            elif k==curses.KEY_PPAGE:
                self.ny=max(0,self.ny-max(1,self.s.getmaxyx()[0]-10)); self.nx=min(self.nx,len(self.notes[self.ny]))
            elif k==curses.KEY_NPAGE:
                self.ny=min(len(self.notes)-1,self.ny+max(1,self.s.getmaxyx()[0]-10)); self.nx=min(self.nx,len(self.notes[self.ny]))
            elif k==curses.KEY_HOME:self.nx=0
            elif k==curses.KEY_END:self.nx=len(self.notes[self.ny])
            elif k==curses.KEY_BACKSPACE:
                if self.nx:self.notes[self.ny]=self.notes[self.ny][:self.nx-1]+self.notes[self.ny][self.nx:]; self.nx-=1
                elif self.ny:self.nx=len(self.notes[self.ny-1]); self.notes[self.ny-1]+=self.notes.pop(self.ny); self.ny-=1
            elif k==curses.KEY_DC:
                self.notes[self.ny]=self.notes[self.ny][:self.nx]+self.notes[self.ny][self.nx+1:]
            elif k in (10,13):  # Enter delivered as a raw key code
                rest=self.notes[self.ny][self.nx:]
                self.notes[self.ny]=self.notes[self.ny][:self.nx]
                self.notes.insert(self.ny+1,rest); self.ny+=1; self.nx=0
            else:return
        self.notes_dirty=True
        bounds,y0,y1=self.pane_bounds()
        pane_rows=max(1,y1-y0)
        if self.wide_mode():
            nx0,nx1=bounds["notes"]
            notes_width=max(1,nx1-nx0+1)
        else:
            notes_width=max(1,self.s.getmaxyx()[1]-3)
        notes_visual=self.notes_visual_row(self.ny,self.nx,notes_width)
        if notes_visual<self.notes_top:self.notes_top=notes_visual
        elif notes_visual>=self.notes_top+pane_rows:self.notes_top=notes_visual-pane_rows+1

    def handle_mouse(self):
        """Handle pane clicks and mouse-wheel scrolling.

        Wheel scrolling moves the viewport by one displayed line without
        changing the insertion point. In the wide layout, the pane under the
        pointer determines whether the writing document or notes are scrolled.
        In the narrow layout, the currently focused pane is scrolled.
        """
        try:
            _id, x, y, _z, b = curses.getmouse()
        except curses.error:
            return

        # ncurses reports the wheel as BUTTON4 (up) and BUTTON5 (down) on
        # common terminals. Accept PRESSED and CLICKED variants where defined.
        wheel_up = b & (getattr(curses, "BUTTON4_PRESSED", 0) |
                        getattr(curses, "BUTTON4_CLICKED", 0))
        wheel_down = b & (getattr(curses, "BUTTON5_PRESSED", 0) |
                          getattr(curses, "BUTTON5_CLICKED", 0))
        if wheel_up or wheel_down:
            delta = -1 if wheel_up else 1
            if self.wide_mode():
                bounds, y0, y1 = self.pane_bounds()
                if not (y0 <= y < y1):
                    return
                name = self.pane_at(x)
                if name == "notes":
                    self.focus = "notes"
                    rows = max(1, y1 - y0)
                    nx0, nx1 = bounds["notes"]
                    notes_width = max(1, nx1 - nx0 + 1)
                    visual_total = sum(max(1, (len(line) + notes_width - 1) // notes_width)
                                       for line in self.notes)
                    self.notes_top = max(0, min(max(0, visual_total - rows),
                                                self.notes_top + delta))
                elif name == "writing":
                    self.focus = "editor"
                    self.top = max(0, min(max(0, len(self.lines) - 1), self.top + delta))
                else:
                    # The context pane is read-only; scrolling over it scrolls
                    # the document viewport so the user can browse its source.
                    self.top = max(0, min(max(0, len(self.lines) - 1), self.top + delta))
            elif self.focus == "notes":
                h, w = self.s.getmaxyx()
                rows = max(1, h - 3 - 5)
                notes_width = max(1, w - 3)
                visual_total = sum(max(1, (len(line) + notes_width - 1) // notes_width)
                                   for line in self.notes)
                self.notes_top = max(0, min(max(0, visual_total - rows),
                                            self.notes_top + delta))
            else:
                self.top = max(0, min(max(0, len(self.lines) - 1), self.top + delta))
            return

        # Left-button events in adjust-view mode drive the rulers. The
        # REPORT bit is included because this ncurses build reports
        # held-button motion with it instead of a BUTTON1_MOVED flag.
        b1_any = (getattr(curses, "BUTTON1_PRESSED", 0) |
                  getattr(curses, "BUTTON1_RELEASED", 0) |
                  getattr(curses, "BUTTON1_CLICKED", 0) |
                  getattr(curses, "BUTTON1_MOVED", 0) |
                  getattr(curses, "MOUSE_MOVED", 0) |
                  getattr(curses, "REPORT_MOUSE_POSITION", 0))
        if self.adjust_view and (b & b1_any):
            self._adjust_mouse(x, y, b)
            return

        # Ordinary left-clicks select the writing or notes pane in wide mode.
        if not (b & (getattr(curses, "BUTTON1_PRESSED", 0) |
                     getattr(curses, "BUTTON1_RELEASED", 0) |
                     getattr(curses, "BUTTON1_CLICKED", 0))):
            return
        if not self.wide_mode():
            return
        bounds, y0, y1 = self.pane_bounds()
        # Clickable area spans the pane header and separator through content.
        if not (y0 - 2 <= y < y1):
            return
        name = self.pane_at(x)
        if name == "writing":
            if self.focus == "notes":
                self.save_notes()
            self.focus = "editor"
            wx0, wx1 = bounds["writing"]
            line = self.top + max(0, y - y0)
            self.y = max(0, min(len(self.lines) - 1, line))
            self.x = max(0, min(len(self.lines[self.y]), x - wx0))
            self.message = "Focus: WRITING (F12 back to notes)"
        elif name == "notes":
            self.focus = "notes"
            nx0, nx1 = bounds["notes"]
            line = self.notes_top + max(0, y - y0)
            self.ny = max(0, min(len(self.notes) - 1, line))
            self.nx = max(0, min(len(self.notes[self.ny]), x - nx0))
            self.message = "Focus: NOTES (F12 back to writing)"
        # The context pane is read-only: clicks there are ignored.

    def quit_flow(self):
        if not self.dirty:
            self.quit_requested=True; return
        self.in_quit_prompt=True
        try:answer=self.prompt("SAVE CHANGES BEFORE QUIT? (Y/N, Esc or Ctrl-C cancels) ")
        finally:self.in_quit_prompt=False
        if answer is None:return
        choice=answer.strip().lower()
        if choice=="y":
            self.save()
            if not self.dirty:self.quit_requested=True
        elif choice=="n":
            original_path=self.path
            try:
                if original_path:self.save_position(original_path)
                backup=self.backup_old(original_path) if original_path else None
                self.path=None
                if self.write_recovery():
                    self.preserve_recovery=True; self.quit_requested=True
                    if backup:self.message=f"Recovery saved; original backed up to {backup}"
                else:self.path=original_path
            except OSError as e:
                self.message=f"Cannot back up original; recovery kept open: {e}"
        else:self.message="Choose Y to save, N to recover later, or Esc to cancel."

    def menu(self,index,expanded=False):
        self.tab=index; items=MENUS[TABS[self.tab]]; selected=0
        while True:
            self.message=(f"{TABS[self.tab]} selected · Left/Right menus · Down/Enter opens · Esc closes"
                          if not expanded else
                          f"{TABS[self.tab]} · Left/Right menus · Up/Down select · Enter choose · Esc closes")
            self.draw(); h,w=self.s.getmaxyx()
            if expanded:
                items=MENUS[TABS[self.tab]]
                labels=[(f"Paper size ({self.page_preset})" if item=="Paper size" else
                         f"Auto capitalisation ({'On' if self.auto_capitalise else 'Off'})" if item=="Auto capitalisation" else item) for item in items]
                width=min(max(map(len,labels))+4,w-4)
                x=min(2+self.tab*max(7,(w-4)//len(TABS)),w-width-2)
                # Box the deployed menu below the menu-bar title. Use the
                # same foreground colour as the editor text, and keep the
                # tab title (FILE/STYLE/SCROLL...) outside the box.
                border_attr=curses.color_pair(1)
                top=4
                self.put(top,x,"┌"+"─"*(width-2)+"┐",border_attr)
                for i,label in enumerate(labels):
                    self.put(top+1+i,x,"│"+(" "+label).ljust(width-2)+"│",
                             curses.color_pair(2) if i==selected else border_attr)
                self.put(top+1+len(labels),x,"└"+"─"*(width-2)+"┘",border_attr)
            self.s.refresh()
            try:k=self.s.get_wch()
            except KeyboardInterrupt:
                # Ctrl+C while browsing a menu should quit WordPro directly,
                # rather than merely closing the menu and resuming the editor.
                self.ctrl_c_pending=False
                self.quit_flow()
                return
            except curses.error:continue
            if k == "\x13":
                self.save()
                return
            if k == "\x0f":
                self.open_file()
                return
            if isinstance(k,int) and curses.KEY_F1<=k<=curses.KEY_F10:
                self.tab=k-curses.KEY_F1; selected=0; expanded=False; continue
            if k in (27,"\x1b"):
                sequence=self.escape_sequence()
                arrow=self.modified_arrow(sequence)
                if arrow is not None:
                    if arrow=="left":self.tab=(self.tab-1)%len(TABS); selected=0
                    elif arrow=="right":self.tab=(self.tab+1)%len(TABS); selected=0
                    elif arrow in ("up","down"):
                        expanded=True; items=MENUS[TABS[self.tab]]
                        selected=(selected+(-1 if arrow=="up" else 1))%len(items)
                    continue
                code=self.modified_key_code(sequence)
                if code is not None:
                    if 49<=code<=57:
                        self.tab=code-49; selected=0; expanded=False
                    elif code==48:  # Ctrl-0 -> the 10th menu (BLOCK)
                        self.tab=9; selected=0; expanded=False
                    else:self.handle_modified_key(code)
                    continue
                return
            if k=="\x04":
                self.go_to_main_menu()
                return
            if k in (curses.KEY_LEFT,):
                self.tab=(self.tab-1)%len(TABS); selected=0
                continue
            if k in (curses.KEY_RIGHT,):
                self.tab=(self.tab+1)%len(TABS); selected=0
                continue
            if k in ("\t",9):
                self.tab=(self.tab+1)%len(TABS); selected=0
                continue
            if not expanded:
                if k in (curses.KEY_DOWN,10,13,"\n","\r"):
                    expanded=True; selected=0
                continue
            items=MENUS[TABS[self.tab]]
            if k in (curses.KEY_UP,ord("k")):selected=(selected-1)%len(items)
            elif k in (curses.KEY_DOWN,ord("j")):selected=(selected+1)%len(items)
            elif k in (10,13,32,"\n","\r"):self.action(items[selected]); return

    def escape_sequence(self):
        chars=[]
        self.s.timeout(50)
        try:
            while True:
                try:c=self.s.get_wch()
                except KeyboardInterrupt:
                    self.ctrl_c_pending=True; break
                except curses.error:break
                if not isinstance(c,str):break
                chars.append(c)
                if c in "~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":break
        finally:self.s.timeout(750)
        return "".join(chars)

    @staticmethod
    def modified_arrow(sequence):
        directions={"A":"up","B":"down","C":"right","D":"left"}
        final=sequence[-1:] if sequence else ""
        if final in directions and (sequence.startswith(("[1;5", "O5")) or
                                    sequence.startswith(("[5", "[6"))):
            return directions[final]
        if sequence.startswith("[") and sequence.endswith("u"):
            try:
                parts=sequence[1:-1].split(";")
                if len(parts)>=2 and int(parts[1].split(":",1)[0])==5:
                    return {57350:"left",57351:"right",57352:"up",57353:"down"}.get(int(parts[0]))
            except ValueError:pass
        return None

    def run(self):
        curses.start_color(); curses.use_default_colors()
        curses.init_pair(1,curses.COLOR_YELLOW,curses.COLOR_BLACK); curses.init_pair(2,curses.COLOR_BLACK,curses.COLOR_YELLOW)
        self.s.bkgd(" ",curses.color_pair(1)); curses.noecho(); curses.curs_set(1); self.s.keypad(True); self.s.timeout(750)
        try:curses.mousemask(curses.ALL_MOUSE_EVENTS|curses.REPORT_MOUSE_POSITION)
        except curses.error:pass
        recovered=self.recover_draft()
        if self.ctrl_c_pending:
            self.ctrl_c_pending=False; self.quit_flow()
            if self.quit_requested:return
        if not recovered:
            self.document_picker()
        if self.ctrl_c_pending:
            self.ctrl_c_pending=False; self.quit_flow()
            if self.quit_requested:return
        next_recovery=time.monotonic()+2
        while True:
            self.draw()
            try:k=self.s.get_wch()
            except KeyboardInterrupt:
                self.quit_flow()
                if self.quit_requested:break
                continue
            except curses.error:
                if self.dirty and time.monotonic()>=next_recovery:
                    self.write_recovery(quiet=True); next_recovery=time.monotonic()+2
                continue
            if self.adjust_view:
                # Adjusting the view: the mouse drives the rulers, Esc ends
                # the mode (and asks about saving the layout). Typing and the
                # other shortcuts are ignored so the document cannot change.
                if k==curses.KEY_MOUSE:
                    self.handle_mouse()
                elif k in (27,"\x1b"):
                    self.end_adjust_view()
                if self.quit_requested:break
                if self.ctrl_c_pending:
                    self.ctrl_c_pending=False; self.quit_flow()
                    if self.quit_requested:break
                continue
            if k==curses.KEY_MOUSE:
                self.handle_mouse()
            elif isinstance(k,str):
                if k=="\x13":self.save()
                elif k=="\x0f":self.open_file()
                elif k=="\x06":
                    # First Ctrl+F prompts; later presses jump to the next match.
                    if self.find_term:
                        self.find_next_match()
                    else:
                        self.action("Find")
                elif k=="\x04":
                    self.go_to_main_menu()
                    continue
                elif k=="\x1b":
                    sequence=self.escape_sequence()
                    arrow=self.modified_arrow(sequence)
                    if arrow in ("left", "right"):
                        self.tab=(self.tab+(-1 if arrow=="left" else 1))%len(TABS)
                        self.menu(self.tab)
                    elif arrow in ("up", "down"):
                        self.menu(self.tab)
                    else:
                        code=self.modified_key_code(sequence)
                        if code is not None:self.handle_modified_key(code)
                    if self.quit_requested:break
                elif self.focus=="notes":
                    self.notes_key(k)
                elif k in ("\n","\r"):
                    if self.lines[self.y]=="\f":
                        self.lines[self.y]=""; self.lines.insert(self.y+1,"\f")
                        fmt=self.line_formats[self.y].copy(); self.line_formats[self.y:self.y+1]=[fmt,fmt.copy()]
                        self.y+=1; self.x=0
                    else:
                        rest=self.lines[self.y][self.x:]; self.lines[self.y]=self.lines[self.y][:self.x]
                        self.lines.insert(self.y+1,rest); self.line_formats.insert(self.y+1,self.line_formats[self.y].copy())
                        self.y+=1; self.x=0
                    self.dirty=True
                elif k in ("\x7f","\b"):
                    if self.lines[self.y]=="\f":self.lines.pop(self.y); self.line_formats.pop(self.y); self.y=min(self.y,len(self.lines)-1); self.x=0; self.dirty=True
                    elif self.x:
                        self.lines[self.y]=self.lines[self.y][:self.x-1]+self.lines[self.y][self.x:]
                        self.x-=1; self.dirty=True; self.wrap_current_line()
                    elif self.y and self.lines[self.y-1]=="\f":self.lines.pop(self.y-1); self.line_formats.pop(self.y-1); self.y-=1; self.x=0; self.dirty=True
                    elif self.y:self.x=len(self.lines[self.y-1]); self.lines[self.y-1]+=self.lines.pop(self.y); self.line_formats.pop(self.y); self.y-=1; self.dirty=True; self.wrap_current_line()
                elif k=="\t":
                    self.lines[self.y]=self.lines[self.y][:self.x]+"    "+self.lines[self.y][self.x:]
                    self.x+=4; self.dirty=True; self.wrap_current_line()
                elif k.isprintable():
                    # Auto-capitalise sentence starts and the standalone pronoun "I".
                    # Apply this before normal insertion so cursor/wrapping logic stays shared.
                    if self.auto_capitalise and self.lines[self.y] != "\f":
                        line = self.lines[self.y]
                        prefix = line[:self.x]
                        if k.isalpha():
                            # Only capitalise at the start of the document, after
                            # a blank paragraph, or after sentence punctuation.
                            # A wrapped/continued line by itself is NOT a sentence.
                            trimmed = prefix.rstrip()
                            at_sentence_start = bool(trimmed and
                                trimmed[-1:] in (".", "!", "?", "…") and len(trimmed) < len(prefix))
                            if not prefix and self.y == 0:
                                at_sentence_start = True
                            elif not prefix and self.y > 0:
                                previous = self.lines[self.y-1]
                                at_sentence_start = (not previous.strip() or previous == "\f" or
                                    bool(previous.rstrip() and previous.rstrip()[-1:] in (".", "!", "?", "…")))
                            if at_sentence_start:
                                k = k.upper()
                        # When a delimiter completes a standalone "i", capitalise it.
                        if k.isspace() or k in (".", ",", "!", "?", ";", ":", ")", "\"", "\'"):
                            match = re.search(r"(?i)(?<![\w])i$", prefix)
                            if match:
                                line = line[:match.start()] + "I" + line[match.end():]
                                self.lines[self.y] = line
                    if self.lines[self.y]=="\f":self.lines[self.y]=k; self.x=1
                    else:
                        self.lines[self.y]=self.lines[self.y][:self.x]+k+self.lines[self.y][self.x:]
                        self.x+=len(k)
                    self.dirty=True; self.wrap_current_line()
            else:
                if curses.KEY_F1<=k<=curses.KEY_F10:
                    self.tab=k-curses.KEY_F1; self.menu(self.tab)
                elif k==curses.KEY_F12:
                    self.focus="notes" if self.focus=="editor" else "editor"
                    if self.focus=="editor":self.save_notes()
                    self.message=("NOTES — F12 goes back to writing" if self.focus=="notes"
                                  else "Writing — F12 for notes")
                elif self.focus=="notes":
                    self.notes_key(k)
                elif k==curses.KEY_LEFT:
                    if self.x>0:self.x-=1
                    elif self.y>0:
                        self.y-=1
                        self.x=len(self.lines[self.y])
                elif k==curses.KEY_RIGHT:
                    if self.x<len(self.lines[self.y]):self.x+=1
                    elif self.y<len(self.lines)-1:
                        self.y+=1
                        self.x=0
                elif k==curses.KEY_UP:self.y=max(0,self.y-1); self.x=min(self.x,len(self.lines[self.y]))
                elif k==curses.KEY_DOWN:self.y=min(len(self.lines)-1,self.y+1); self.x=min(self.x,len(self.lines[self.y]))
                elif k==curses.KEY_PPAGE:
                    self.y=max(0,self.y-max(1,self.s.getmaxyx()[0]-9)); self.x=min(self.x,len(self.lines[self.y]))
                elif k==curses.KEY_NPAGE:
                    self.y=min(len(self.lines)-1,self.y+max(1,self.s.getmaxyx()[0]-9)); self.x=min(self.x,len(self.lines[self.y]))
                elif k==curses.KEY_HOME:self.x=0
                elif k==curses.KEY_END:self.x=len(self.lines[self.y])
                elif k==curses.KEY_BACKSPACE:
                    if self.lines[self.y]=="\f":self.lines.pop(self.y); self.line_formats.pop(self.y); self.y=min(self.y,len(self.lines)-1); self.x=0; self.dirty=True
                    elif self.x:
                        self.lines[self.y]=self.lines[self.y][:self.x-1]+self.lines[self.y][self.x:]
                        self.x-=1; self.dirty=True; self.wrap_current_line()
                    elif self.y and self.lines[self.y-1]=="\f":self.lines.pop(self.y-1); self.line_formats.pop(self.y-1); self.y-=1; self.x=0; self.dirty=True
                    elif self.y:self.x=len(self.lines[self.y-1]); self.lines[self.y-1]+=self.lines.pop(self.y); self.line_formats.pop(self.y); self.y-=1; self.dirty=True; self.wrap_current_line()
                elif k==curses.KEY_DC:
                    if self.lines[self.y]=="\f":self.lines.pop(self.y); self.line_formats.pop(self.y); self.y=min(self.y,len(self.lines)-1); self.x=0
                    else:
                        self.lines[self.y]=self.lines[self.y][:self.x]+self.lines[self.y][self.x+1:]
                    self.dirty=True; self.wrap_current_line()
            if self.quit_requested:break
            if self.ctrl_c_pending:
                self.ctrl_c_pending=False; self.quit_flow()
                if self.quit_requested:break
                continue
            if self.dirty and time.monotonic()>=next_recovery:
                self.write_recovery(quiet=True); next_recovery=time.monotonic()+2
            if self.notes_dirty:self.save_notes()
            self.anchor_view()
        if self.path:self.save_position()
        if self.notes_dirty:self.save_notes()
        if not self.preserve_recovery:self.clear_recovery()
        self.unmount_all()

def main(screen):
    fd=0
    original=termios.tcgetattr(fd)
    current=original[:]
    current[0] &= ~termios.IXON
    termios.tcsetattr(fd,termios.TCSANOW,current)
    try:WordProcessor(screen).run()
    finally:termios.tcsetattr(fd,termios.TCSANOW,original)
if __name__=="__main__":
    import sys
    try:
        sys.stdout.write("\x1b[>4;2m"); sys.stdout.flush()
        curses.wrapper(main)
    finally:
        sys.stdout.write("\x1b[>4;0m"); sys.stdout.flush()
