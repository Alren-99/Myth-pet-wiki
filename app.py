"""Conservative Windows PnP recovery UI. Standard library only."""
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

WINDOWS = sys.platform == 'win32'
TARGET = re.compile(r'(?:AMD\s+)?Radeon\s*(?:\(TM\)\s*)?RX\s+7800\s+XT', re.I)
LOG_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AMD GPU Recovery Tool' / 'Logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger('recovery')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_DIR / 'recovery.log', maxBytes=2_000_000, backupCount=4, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(handler)


def system_exe(name):
    return str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / name)


def run(args, timeout=120):
    logger.info('Executando: %s', subprocess.list2cmdline(args))
    result = subprocess.run(args, capture_output=True, timeout=timeout,
                            creationflags=0x08000000 if WINDOWS else 0)
    # PowerShell explicitly emits UTF-8; pnputil uses the Windows console encoding.
    encoding = 'utf-8' if 'powershell' in args[0].lower() else ('oem' if WINDOWS else 'utf-8')
    output = (result.stdout + result.stderr).decode(encoding, errors='replace').strip()
    logger.info('Saída (%s): %s', result.returncode, output)
    if result.returncode not in (0, 3010):
        raise RuntimeError(f'Comando falhou (código {result.returncode}).\n{output}')
    return output, result.returncode


def ps(script):
    output, _ = run([system_exe(r'WindowsPowerShell\v1.0\powershell.exe'),
                     '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
                     "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); $ErrorActionPreference='Stop'; " + script])
    return output


def inventory():
    raw = ps(r"""
$items = @(Get-PnpDevice -PresentOnly | ForEach-Object {
  $d = $_
  if ($d.InstanceId -like 'PCI\VEN_1002&*') {
    $ids = @((Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_HardwareIds').Data)
    $code = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode').Data
    [PSCustomObject]@{name=$d.FriendlyName; id=$d.InstanceId; class=$d.Class; code=[int]$code; hardware=$ids}
  }
})
ConvertTo-Json -InputObject $items -Depth 5 -Compress
""")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError('Inventário PnP inesperado; nenhuma alteração foi feita.')
    return data


def exact_target(device):
    return (device['class'] == 'Display' and
            TARGET.fullmatch(device.get('name') or '') is not None and
            device['id'].upper().startswith('PCI\\VEN_1002&') and
            any(re.match(r'^PCI\\VEN_1002&DEV_[0-9A-F]{4}(?:&|$)', h, re.I)
                for h in device.get('hardware', [])))


def verify_inf(inf):
    # SetupAPI checks the INF's signed catalog using the current Windows trust policy.
    class Signer(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('CatalogFile', wintypes.WCHAR * 260),
                    ('DigitalSigner', wintypes.WCHAR * 260), ('DigitalSignerVersion', wintypes.WCHAR * 260)]
    info = Signer()
    info.cbSize = ctypes.sizeof(info)
    api = ctypes.WinDLL('setupapi', use_last_error=True)
    verify = api.SetupVerifyInfFileW
    verify.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(Signer)]
    verify.restype = wintypes.BOOL
    if not verify(str(inf), None, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    logger.info('INF validado por SetupAPI: %s; catálogo: %s; assinante: %s', inf, info.CatalogFile, info.DigitalSigner)


def compatible_inf(inf, devices):
    raw = inf.read_bytes()
    text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig', errors='replace')
    sections = {}
    section = ''
    for line in text.splitlines():
        line = line.split(';', 1)[0].strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].lower()
            sections.setdefault(section, [])
        elif line:
            sections.setdefault(section, []).append(line)
    version = '\n'.join(sections.get('version', []))
    if not re.search(r'^Class\s*=\s*Display\s*$', version, re.I | re.M):
        raise ValueError('O INF deve declarar a classe Display.')
    if not re.search(r'^ClassGuid\s*=\s*\{4d36e968-e325-11ce-bfc1-08002be10318\}\s*$', version, re.I | re.M):
        raise ValueError('GUID de classe de vídeo ausente ou incompatível.')
    provider_match = re.search(r'^Provider\s*=\s*(.+)$', version, re.I | re.M)
    provider = provider_match.group(1).strip().strip('"') if provider_match else ''
    if provider.startswith('%') and provider.endswith('%'):
        key = provider[1:-1].lower()
        values = {}
        for line in sections.get('strings', []):
            if '=' in line:
                k, v = line.split('=', 1)
                values[k.strip().lower()] = v.strip().strip('"')
        provider = values.get(key, '')
    if provider.casefold() not in {'advanced micro devices, inc.', 'advanced micro devices, inc', 'amd', 'ati technologies inc.'}:
        raise ValueError('Fornecedor AMD não confirmado no INF. Pacote recusado.')
    # Only model sections explicitly declared by Manufacturer for x64 are considered.
    model_sections = set()
    for line in sections.get('manufacturer', []):
        if '=' not in line:
            continue
        tokens = [s.strip().strip('"').lower() for s in line.split('=', 1)[1].split(',')]
        for decoration in tokens[1:]:
            if decoration.startswith('ntamd64'):
                model_sections.add(tokens[0] + '.' + decoration)
    supported = set()
    for name in model_sections:
        for line in sections.get(name, []):
            if '=' not in line:
                continue
            parts = [s.strip().strip('"').upper() for s in line.split('=', 1)[1].split(',')]
            supported.update(parts[1:])
    matches = [d for d in devices if any(h.upper() in supported for h in d['hardware'])]
    if not matches:
        raise ValueError('Nenhum hardware ID PCI AMD presente corresponde exatamente aos modelos x64 deste INF.')
    # Windows validates OS applicability, package files, signature, and ranking at installation.
    verify_inf(inf)
    return matches


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('AMD GPU Recovery Tool')
        self.geometry('1040x760')
        self.minsize(900, 660)
        self.configure(bg='#101622')
        self.events = queue.Queue()
        self.busy = False
        self.simulate = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='Pronto • simulação ativada')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', background='#101622', foreground='#e6edf7', font=('Segoe UI', 10))
        style.configure('TButton', background='#263750', padding=(14, 10))
        style.map('TButton', background=[('active', '#365276')])
        style.configure('TCheckbutton', background='#101622')
        style.configure('Treeview', background='#192335', fieldbackground='#192335', rowheight=30)
        style.configure('Treeview.Heading', background='#263750')
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='AMD GPU Recovery Tool', font=('Segoe UI', 24, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Radeon RX 7800 XT • Windows 11', foreground='#90b8ee').pack(anchor='w', pady=(4, 16))
        ttk.Label(frame, text='Software não corrige falhas de enumeração PCIe, alimentação ou hardware.\n'
                  'Sem dispositivo PCI AMD presente, a instalação de driver permanece bloqueada.',
                  wraplength=950).pack(anchor='w', pady=(0, 16))
        self.check = ttk.Checkbutton(frame, text='Modo de simulação — registra ações sem alterar dispositivos', variable=self.simulate)
        self.check.pack(anchor='w')
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=16)
        self.buttons = []
        for title, action in [('Detectar dispositivos', self.detect), ('Revarrer Plug and Play', self.scan),
                              ('Habilitar RX 7800 XT', self.enable), ('Instalar driver local…', self.install)]:
            button = ttk.Button(bar, text=title, command=action)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        self.tree = ttk.Treeview(frame, columns=('name', 'code', 'id'), show='headings', height=6)
        for col, title, width in [('name', 'Dispositivo AMD presente', 250), ('code', 'Código PnP', 90), ('id', 'Instance ID', 480)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width)
        self.tree.pack(fill='x')
        ttk.Label(frame, text='Código 22 = desabilitado. Código 0 = sem problema PnP informado. Outros códigos exigem diagnóstico.',
                  wraplength=950).pack(anchor='w', pady=8)
        ttk.Label(frame, textvariable=self.status, foreground='#90b8ee', wraplength=950).pack(anchor='w', pady=8)
        self.output = tk.Text(frame, bg='#0b1019', fg='#d0dced', relief='flat', font=('Consolas', 10), height=10, state='disabled', wrap='word')
        self.output.pack(fill='both', expand=True)
        footer = ttk.Frame(frame)
        footer.pack(fill='x', pady=(12, 0))
        restore = ttk.Button(footer, text='Abrir Restauração do Sistema…', command=self.restore)
        restore.pack(side='left')
        self.buttons.append(restore)
        ttk.Label(footer, text=f'Logs: {LOG_DIR}', wraplength=620).pack(side='right')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)
        self.note('Nenhum driver será removido. Registro e Windows Update não serão alterados.')
        if not WINDOWS:
            self.note('Plataforma atual não é Windows. Somente simulação disponível.')
            self.check.state(['disabled'])

    def note(self, text):
        logger.info(text)
        self.output.configure(state='normal')
        self.output.insert('end', text + '\n')
        self.output.see('end')
        self.output.configure(state='disabled')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'inventory':
                    self.tree.delete(*self.tree.get_children())
                    for d in value:
                        self.tree.insert('', 'end', values=(d['name'], d['code'], d['id']))
                elif kind == 'done':
                    self.busy = False
                    for button in self.buttons:
                        button.state(['!disabled'])
                    if WINDOWS:
                        self.check.state(['!disabled'])
                    self.status.set(value)
                    self.note(value)
                else:
                    self.note(value)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def job(self, action):
        if self.busy:
            return
        self.busy = True
        simulation = self.simulate.get()
        for button in self.buttons:
            button.state(['disabled'])
        self.check.state(['disabled'])
        self.status.set('Executando…')
        def worker():
            try:
                message = action(simulation)
            except subprocess.TimeoutExpired:
                logger.exception('Tempo limite')
                message = 'Tempo limite excedido. O estado pode ter mudado; detecte novamente antes de repetir.'
            except Exception as exc:
                logger.exception('Falha')
                message = f'Falha: {exc}'
            self.events.put(('done', message))
        threading.Thread(target=worker, daemon=True).start()

    def read_devices(self):
        if not WINDOWS:
            raise RuntimeError('Detecção real disponível somente no Windows; nenhum dispositivo fictício é apresentado.')
        devices = inventory()
        self.events.put(('inventory', devices))
        return devices

    def detect(self):
        self.job(lambda sim: f'{len(self.read_devices())} dispositivo(s) PCI AMD presente(s). Inventário somente leitura.')

    def scan(self):
        def action(sim):
            if sim:
                return 'SIMULAÇÃO: executaria pnputil /scan-devices. Nenhuma alteração realizada.'
            run([system_exe('pnputil.exe'), '/scan-devices'])
            devices = self.read_devices()
            return f'Revarredura concluída: {len(devices)} dispositivo(s) PCI AMD presente(s). Ausência de enumeração não pode ser corrigida por este aplicativo.'
        self.job(action)

    def enable(self):
        def action(sim):
            devices = self.read_devices()
            targets = [d for d in devices if exact_target(d) and d['code'] == 22]
            if not targets:
                return 'Nenhuma RX 7800 XT inequivocamente identificada e desabilitada (código 22). Nada alterado.'
            for d in targets:
                if sim:
                    self.events.put(('log', f'SIMULAÇÃO: habilitaria {d["id"]}'))
                else:
                    # Recheck presence, identity and disabled state immediately before mutation.
                    current = next((x for x in inventory() if x['id'] == d['id']), None)
                    if not current or not exact_target(current) or current['code'] != 22:
                        raise RuntimeError('Estado do dispositivo mudou; habilitação cancelada.')
                    run([system_exe('pnputil.exe'), '/enable-device', d['id']])
            if sim:
                return 'Simulação de habilitação concluída; nada alterado.'
            remaining = self.read_devices()
            if any(not any(x['id'] == d['id'] and x['code'] == 0 for x in remaining) for d in targets):
                return 'Comando de habilitação executado, mas recuperação não confirmada. Consulte o código PnP e os logs.'
            return 'Dispositivo(s) habilitado(s), código PnP 0. Isso não comprova funcionamento físico da GPU.'
        self.job(action)

    def install(self):
        path = filedialog.askopenfilename(title='Selecione o INF do pacote AMD extraído', filetypes=[('Driver INF', '*.inf')])
        if not path:
            return
        if not self.simulate.get() and not messagebox.askyesno('Instalar pacote local',
                'Adicionar este pacote ao Driver Store e solicitar instalação nos dispositivos compatíveis?\n\n'
                'O Windows verifica assinatura, compatibilidade e prioridade. O pacote pode atender outros dispositivos compatíveis. '
                'Nenhum driver será removido nem terá sua prioridade forçada. Pode ser necessário reiniciar.'):
            return
        def action(sim):
            devices = self.read_devices()
            if not devices:
                return 'Bloqueado: nenhum dispositivo PCI AMD presente. Não há alvo compatível detectado.'
            source = Path(path).resolve()
            if source.suffix.lower() != '.inf' or not source.is_file():
                raise ValueError('Selecione um arquivo INF local existente.')
            # Stage a private snapshot; validate the exact INF that will be passed to PnPUtil.
            with tempfile.TemporaryDirectory(prefix='amd-recovery-') as temp:
                package = Path(temp) / 'package'
                shutil.copytree(source.parent, package, symlinks=False)
                inf = package / source.name
                matches = compatible_inf(inf, devices)
                self.events.put(('log', f'Pacote assinado validado; {len(matches)} dispositivo(s) com hardware ID exato.'))
                if sim:
                    return 'SIMULAÇÃO: pacote validado; executaria pnputil /add-driver <INF> /install. Nada instalado.'
                compatible_inf(inf, inventory())
                _, code = run([system_exe('pnputil.exe'), '/add-driver', str(inf), '/install'], timeout=600)
                self.read_devices()
                return ('Pacote processado. Reinicialização necessária.' if code == 3010 else
                        'Pacote processado pelo Windows. Isso não garante troca de driver: o Windows mantém a seleção por compatibilidade e prioridade. Consulte os logs.')
        self.job(action)

    def restore(self):
        if not messagebox.askyesno('Restauração do Sistema',
                                  'Abrir a ferramenta de Restauração do Sistema do Windows? '
                                  'Nenhuma restauração será iniciada automaticamente.'):
            return
        def action(sim):
            if sim:
                return 'SIMULAÇÃO: abriria rstrui.exe. Nenhuma restauração iniciada.'
            subprocess.Popen([system_exe('rstrui.exe')])
            return 'Ferramenta de Restauração do Sistema aberta. A decisão de restaurar pertence ao usuário.'
        self.job(action)

    def close(self):
        if self.busy:
            messagebox.showinfo('Operação em andamento', 'Aguarde a conclusão para preservar o resultado e os logs.')
        else:
            self.destroy()


def main():
    if WINDOWS and not ctypes.windll.shell32.IsUserAnAdmin():
        args = sys.argv[1:] if getattr(sys, 'frozen', False) else [str(Path(__file__).resolve()), *sys.argv[1:]]
        result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable,
                                                     subprocess.list2cmdline(args), None, 1)
        if result <= 32:
            root = tk.Tk(); root.withdraw()
            messagebox.showerror('Administrador necessário', 'Elevação cancelada ou recusada. Nenhuma ação foi executada.')
            root.destroy()
        return
    App().mainloop()


if __name__ == '__main__':
    main()
