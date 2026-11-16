"""Local credential entry. Never load saved secret values into the form."""
from __future__ import annotations

import sys
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import keyring

SERVICE = 'CareerScout'


class CredentialWindow:
    def __init__(self, root):
        self.root = root
        root.title('AI 채용 탐색 — 인증 정보 등록')
        root.geometry('720x580')
        root.minsize(650, 550)
        root.option_add('*Font', ('맑은 고딕', 10))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text='여기에 인증 정보를 입력하세요', font=('맑은 고딕', 17, 'bold')).grid(
            row=0, column=0, sticky='w', pady=(0, 12))
        ttk.Label(frame, text='두 입력칸에 붙여넣고 아래 [저장]을 누르면 됩니다.\n'
                  '값은 이 PC의 Windows 자격 증명에 저장됩니다.').grid(row=1, column=0, sticky='w')
        ttk.Label(frame, text='1. OpenAI API 키', font=('맑은 고딕', 11, 'bold')).grid(
            row=2, column=0, sticky='w', pady=(22, 6))
        self.api = ttk.Entry(frame, show='●')
        self.api.grid(row=3, column=0, sticky='ew', ipady=5)
        ttk.Label(frame, text='sk-로 시작하는 새 API 키 전체를 붙여넣으세요.').grid(
            row=4, column=0, sticky='w', pady=(5, 0))
        ttk.Label(frame, text='2. Gmail 앱 비밀번호', font=('맑은 고딕', 11, 'bold')).grid(
            row=5, column=0, sticky='w', pady=(22, 6))
        self.gmail = ttk.Entry(frame, show='●')
        self.gmail.grid(row=6, column=0, sticky='ew', ipady=5)
        ttk.Label(frame, text='config/settings.yaml의 recipient 계정에서 만든 16자리 앱 비밀번호입니다.\n'
                  'Google 로그인 비밀번호가 아닙니다. 중간 공백은 자동으로 제거합니다.').grid(
            row=7, column=0, sticky='w', pady=(5, 8))
        ttk.Button(frame, text='Gmail 앱 비밀번호 만들기 (Google 페이지 열기)',
                   command=lambda: webbrowser.open('https://myaccount.google.com/apppasswords')).grid(
                       row=8, column=0, sticky='w')
        ttk.Label(frame, text='발급 메뉴가 없으면 Google 계정의 2단계 인증부터 설정하세요.').grid(
            row=9, column=0, sticky='w', pady=(5, 10))
        self.status = ttk.Label(frame, text='', wraplength=650)
        self.status.grid(row=10, column=0, sticky='w', pady=(8, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, sticky='e')
        ttk.Button(buttons, text='저장', command=self.save).pack(side='left', padx=6)
        ttk.Button(buttons, text='닫기', command=root.destroy).pack(side='left')
        self.refresh()
        self.api.focus_set()

    def registered(self, name):
        try:
            return bool(keyring.get_password(SERVICE, name))
        except Exception:
            return False

    def refresh(self):
        api = '등록됨' if self.registered('openai_api_key') else '미등록'
        gmail = '등록됨' if self.registered('gmail_app_password') else '미등록'
        self.status.configure(text=f'현재 저장 상태: OpenAI {api} / Gmail {gmail}\n'
                              '이미 등록된 항목은 빈칸으로 두면 기존 값이 유지됩니다.')

    def save(self):
        if sys.platform != 'win32' or 'WinVault' not in type(keyring.get_keyring()).__name__:
            messagebox.showerror('저장할 수 없음', 'Windows 자격 증명 저장소에서 실행해 주세요.', parent=self.root)
            return
        api = self.api.get().strip()
        gmail = ''.join(self.gmail.get().split())
        if api and (not api.startswith('sk-') or not api.isascii() or any(c.isspace() for c in api)):
            messagebox.showerror('OpenAI 키 확인', 'sk-로 시작하는 API 키 전체를 공백 없이 붙여넣으세요.', parent=self.root)
            return
        if gmail and (len(gmail) != 16 or not gmail.isascii() or not gmail.isalnum()):
            messagebox.showerror('Gmail 앱 비밀번호 확인', 'Google에서 발급한 16자리 앱 비밀번호를 입력하세요.', parent=self.root)
            return
        if not api and not gmail:
            messagebox.showinfo('입력 필요', '등록하거나 변경할 값을 입력해 주세요.', parent=self.root)
            return
        try:
            if api:
                keyring.set_password(SERVICE, 'openai_api_key', api)
            if gmail:
                keyring.set_password(SERVICE, 'gmail_app_password', gmail)
        except Exception:
            self.refresh()
            messagebox.showerror('저장 실패', 'Windows 자격 증명에 저장하지 못했습니다. 현재 저장 상태를 확인해 주세요.', parent=self.root)
            return
        self.api.delete(0, 'end')
        self.gmail.delete(0, 'end')
        self.refresh()
        messagebox.showinfo('저장 완료', '인증 정보를 저장했습니다.\n창을 닫고 현재 폴더의 [지금실행.cmd]를 실행하세요.\n'
                            '실제 API 연결과 메일 발송은 실행 시 확인됩니다.', parent=self.root)


def main():
    root = tk.Tk()
    if '--self-check' in sys.argv:
        root.withdraw()
    app = CredentialWindow(root)
    if '--self-check' in sys.argv:
        root.update_idletasks()
        assert app.api.get() == '' and app.gmail.get() == ''
        assert app.api.cget('show') and app.gmail.cget('show')
        print('Credential window OK: two empty masked inputs; saved secrets are not displayed.')
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
