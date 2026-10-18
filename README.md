# AMD GPU Recovery Tool

Aplicativo Python com interface escura em português para Windows 11 x64. Usa somente a biblioteca padrão em execução. Inicie com `py -3 app.py`; solicita elevação UAC antes de abrir. O modo de simulação vem ativado e não altera dispositivos. A detecção continua sendo real, somente leitura; não são inventados dispositivos para demonstrar sucesso.

## Executável Windows

Em Windows x64 com Python 3.12 ou superior instalado, execute `powershell -ExecutionPolicy RemoteSigned -File .\build.ps1` na pasta do projeto. O resultado é `dist\AMD GPU Recovery Tool.exe`, com manifesto de administrador. A compilação não assina o executável: distribuição pública pode exigir assinatura de código própria. Não há etapa de testes no script nem no fluxo principal.

## Comportamento

- Detecta dispositivos PCI AMD presentes via PowerShell/PnP, exibindo nome, Instance ID e código de problema.
- Executa `pnputil /scan-devices`, sem promessa de recuperar hardware não enumerado.
- Habilita somente dispositivos da classe Display, com nome completo AMD Radeon RX 7800 XT (ou Radeon RX 7800 XT), hardware ID PCI AMD e código 22. Revalida o alvo imediatamente antes da ação. Nomes genéricos, GPUs de outro modelo e dispositivos ausentes são recusados; não infere RX 7800 XT apenas pelo chip compartilhado.
- Aceita um INF de pacote local extraído. Copia a pasta do INF para um diretório temporário privado e exige classe/GUID de vídeo, fornecedor AMD explícito, seção de modelos x64 declarada e correspondência exata com hardware ID presente. Pacotes com estrutura não reconhecida são recusados conservadoramente. Se os arquivos estiverem em pastas ancestrais, use um pacote extraído autocontido; arquivos faltantes serão recusados pelo Windows.
- Valida o INF assinado via `SetupVerifyInfFileW`; o próprio `pnputil /add-driver <INF> /install` volta a verificar assinatura, integridade dos arquivos e aplicabilidade ao Windows antes da instalação. Nunca usa opções para forçar prioridade, remover drivers ou ignorar assinatura. O Windows pode manter um driver de prioridade maior. O comando pode instalar o pacote em outros dispositivos compatíveis presentes, conforme explicitado na confirmação.
- Abrir Restauração do Sistema exige confirmação. Apenas abre `rstrui.exe`; a restauração é decidida dentro da ferramenta do Windows.
- Não remove drivers, não edita registro, não altera Windows Update e não reinicia automaticamente. Não implementa ações que dependam de consentimento separado para essas alterações.

Logs rotativos ficam em `%LOCALAPPDATA%\AMD GPU Recovery Tool\Logs\recovery.log`. Incluem saídas dos comandos, identificadores de dispositivos e caminhos locais; revise antes de compartilhar. O aplicativo apresenta falhas e solicita nova detecção quando há timeout; não interpreta timeout como sucesso.

**Limite físico:** falhas de enumeração PCIe, alimentação, encaixe, firmware ou defeito de hardware não podem ser corrigidas por este software. Código PnP 0 não comprova funcionamento físico ou estabilidade gráfica. A instalação de driver é bloqueada se não houver dispositivo PCI AMD compatível detectado.

## Estado de validação

Sintaxe Python verificada no ambiente Linux de desenvolvimento. Operações Windows, UAC, interface no Windows e geração do `.exe` exigem Windows e não foram executadas neste ambiente. Nenhum binário Windows pré-compilado está incluído.
