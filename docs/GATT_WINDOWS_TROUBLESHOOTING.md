# EasyChange GATT on Windows after the ESP32-S3 firmware update

The original ESP32 HID Bluetooth name is `BT KeyboardMouse E` and the new
encrypted data service is attached to the **same** ESP and Bluetooth device.
USB COM4 stays physically connected to the MCP_HOST controller, not REMOTE_PC.
The GATT client runs in the existing remote EasyChange Qt GUI and is not a
second MCP server.

After flashing firmware 3.0.0, the Windows HID bond might remain connected
even if the firmware's new custom GATT service is not in the Windows cached
attribute database. Because connected HID devices may stop advertising, a
regular `BleakScanner.discover()` scan might also return nothing.

The remote EasyChange GATT client now:

- Resolves the existing *paired Bluetooth LE* device through Windows WinRT
  pairing enumeration, checking the exact device name before connecting;
  refuses multiple addresses with the same name instead of choosing one.
- Uses an existing Bleak BLEDevice reference rather than triggering an
  implicit second BLE advertisement scan.
- Requests `winrt={"use_cached_services": False}` for fresh GATT services,
  characteristics and descriptors. This does not unpair the HID device.
- Never overwrites a BLE/GATT failure with a generic `GATT desconectado`
  immediately afterwards. The Qt label retains the specific failure and the
  stage (`BUSCA_BLE`, `CONEXAO_BLE`, `INSCRICAO_GATT`,
  `CONECTADO`), with word wrap and copyable diagnostic text.
- Keeps QR and encrypted application SAS pairing policies unchanged. Bluetooth
  pairing alone is never considered authentication for EasyChange commands.

If the remote EasyChange still reports a missing GATT characteristic or
service after updating, remove **only** `BT KeyboardMouse E` from the
Windows Bluetooth device list, pair it again (to refresh the Windows bond and
service cache), reopen EasyChange, and retry pairing via the local MCP GUI.
This will briefly interrupt the HID/QR transport. No ESP firmware flash and
no Windows administrator permission are required for this application-level
troubleshooting. If scanning and paired enumeration fail, you may configure
`EASYCHANGE_BLE_DEVICE_ADDRESS` with the **actual** Bluetooth LE MAC
address for this ESP and reopen EasyChange. Never guess an address.

Follow status displayed on the remote EasyChange:

- `BUSCA_BLE`: device pairing/discovery problem.
- `CONEXAO_BLE`: WinRT connection, device reachability or service database.
- `INSCRICAO_GATT`: subscribe to custom notification characteristic failed;
  common when Windows still holds a stale GATT cache.
- `CONECTADO`: physical GATT link was opened; only successful SAS comparison
  and authenticated receipt can make it secure and ready for execution.


### Invalid parameter count during BUSCA_BLE (WinRT)

Some WinRT Python projections reject the one-filter
`DeviceInformation.find_all_async(aqs_filter)` overload with
`TypeError: Invalid parameter count`. The client now retries the two-argument
WinRT signature before resorting to an unfiltered enumeration that must prove
`DeviceInformation.pairing.is_paired`. It then falls back to the normal BLE
scanner when paired-device enumeration itself cannot run. No unpaired item
from an unfiltered enumeration may be used for GATT. These alternatives are
read-only OS discovery, and don't unpair HID, flash the ESP, or bypass SAS.


### Paired device connected as HID but absent from discovery

Windows Bluetooth paired-device AQS selectors identify
`DeviceInformationKind.ASSOCIATION_ENDPOINT`, not the default device
interface kind. The EasyChange client now requests the association endpoint
explicitly via the three-argument WinRT enumeration API, falling back only
when the binding lacks that overload.

If no device was discovered, the displayed BUSCA_BLE error includes counts
`records` (Windows records enumerated; NOT necessarily paired), `named`
(records matching the ESP HID name), `verified` (unique LE addresses whose
parent LE object's pairing status was proved), `le` (verified BluetoothLEDevice instances), `aep` (verified paired
association-endpoint addresses), `source` (WinRT overload that actually
produced the records), and `scan` (raw advertisements seen by Bleak). These counters do not
identify a device by name alone or bypass secure SAS verification.

Some PyWinRT builds require `None` for the nullable collection of extra
properties when requesting `DeviceInformationKind.ASSOCIATION_ENDPOINT`.
The client tries the strict AEP selector with a null property list as well
as Python list/tuple variants BEFORE falling back to the unfiltered device
interface enumeration (which can contain over 1,000 records and duplicate
names with `is_paired=False`). For a named child-interface row, only
`BluetoothLEDevice.device_information.pairing.is_paired` (or a strict
paired-only parent AEP result with no exposed pairing property) provides
authority to resolve that address. Duplicates with different addresses are
rejected, not guessed.


When Windows returns a paired BLE AssociationEndpoint that includes
`System.Devices.Aep.DeviceAddress`, the client verifies its exact display
name, `pairing.is_paired`, and six-octet MAC address before using it.
That avoids an optional `BluetoothLEDevice.from_id_async` call from a
non-UI background thread. A child-interface with the same display name is
never sufficient; unpaired or malformed records cannot be used.


### ATT 0x03 Write Not Permitted when starting notifications

This happens *after* Bluetooth discovery and GATT connection and does not
prove an administrator-only corporate policy. The BLE ATT server rejects
the CCCD subscription write (or Windows is using a stale attribute handle).
For a non-admin remote Windows machine, the application now reports the
read-only GATT inventory (custom service, expected notify characteristic,
notify property, descriptor 0x2902 and optional handle) alongside the
original Bleak exception.

- `notify_char=0` or `notify=0` or `cccd=0` is strong evidence that
  Windows has discovered an incompatible or stale service definition.
- `service=1,notify_char=1,notify=1,cccd=1` proves that those GATT
  attributes were visible, **not** that the remote operating-system policy
  allows them, or that the CCCD's server-side write permissions are correct.
- Do not manually write CCCD from code or downgrade BLE security.
- No administrator or local policy changes are required for this diagnostic.
  If the cache remains stale, an operator may remove and re-pair only this
  ESP from ordinary Windows Bluetooth Settings, understanding this temporarily
  interrupts HID/QR, then relaunch EasyChange. Inspect again before suggesting
  firmware modifications.


### Janela detalhada de diagnóstico sem administrador

A interface gráfica do EasyChange remoto agora mostra **Diagnóstico GATT**
ao lado do status Bluetooth. O botão abre uma única janela independente
com texto técnico ao vivo, mesmo quando não existe conexão GATT. O histórico
começa na abertura da GUI e guarda até 750 eventos em memória (sem log em
disco por padrão). Contém data/hora local com milissegundos, versão do
Bleak/Python, tentativa, origem da descoberta WinRT, estágio, timeout,
dados de descoberta do serviço e características/descritores UUID e handles,
CCCD 0x2902, motivo do encerramento e o traceback completo e limitado de
falhas da biblioteca.

A janela atualiza e rola automaticamente para o final, e há **Copiar log**
e **Salvar log .txt** (salvamento somente após escolha manual). Desse modo o
usuário consegue compartilhar o texto legível do erro sem fotos.

A coleta não usa administrador, não altera a configuração Bluetooth do
Windows, não cria outra conexão BLE, não interfere no HID/QR nem faz flash
do ESP32. Não armazena código SAS, chaves, payloads criptografados nem
texto dos comandos MCP; os campos de credenciais conhecidos são filtrados.
A tentativa de \`start_notify\` continua sujeita às permissões normais da
pilha GATT; a janela é exclusivamente de diagnóstico.


### Leitura remota do log GATT via HID/HDMI existente

A GUI EasyChange liga seu proprio buffer de diagnosticos ao CommandService,
sem abrir outra porta BLE, outra instancia ou filesystem remoto. Pela
ferramenta MCP ja instalada `easychange_exec`, consulte
`:gatt-log 0 8`. A resposta `data.events` tem data/hora com
milissegundos, etapa, mensagem sanitizada, cursor e indicador de eventos
descartados. Para continuar de onde parou, use o `data.cursor` na proxima
solicitacao: `:gatt-log 145 8` (substitua 145 pelo cursor retornado).
Use uma nova sequencia EC1 a cada poll para nao recuperar um snapshot
anterior do journal de idempotencia. O limite por leitura e 1..24 eventos.
A alternativa estruturada `:j1 {"op":"operation","type":"gatt_diagnostics_tail",
"after":0,"limit":8}` tem a mesma semantica e nao exige nova ferramenta MCP.

Estas sao operacoes read-only compatíveis com QR/HDMI existente e
nao abrem outra conexao BLE. A inicializacao/atualizacao permanece a
operacao EasyChange existente `:self-update`, com protecao de editor nao salvo.
Nao executar o reinicio sem verificar o recibo, e evitar reenviar a mesma
sequencia mutante quando o recibo estiver incerto.


### Tratamento de ATT 0x03 persistente no EasyChange (sem administrador)

O backend WinRT/Bleak ativa notificacoes escrevendo no CCCD 0x2902.
Se o GATT server responder ATT 0x03 ('Write Not Permitted'), a aplicacao
nao tem como transformar essa rejeicao do servidor em sucesso apenas por
outra chamada de escrita. A biblioteca Bleak tambem desautoriza escrever
CCCD diretamente. Nao trocar para `force_indicate`: nosso canal anuncia
apenas `notify`.

O EasyChange agora registra um snapshot opcional **somente de leitura** do
CCCD sem cache (se o Windows aceitar leitura), preserva o traceback e
tenta uma unica reconexao limpa. A segunda recusa identica interrompe
as repeticoes automaticas, marca `CCCD_ATT03_ACTION_REQUIRED` e informa
na janela principal que a sessao GATT NAO foi autenticada. O botao
**Tentar GATT** faz uma nova tentativa comecando pelo discovery BLE,
sem direitos de administrador, sem remover o emparelhamento HID nem
alterar permissoes do Windows.

Isto corrige o retry infinito e melhora a separacao entre falha de
transporte e autorizacao. Nao afirma reparar permissoes do servidor GATT:
a causa fisica da recusa continua necessitando verificacao caso persista.
O QR/HID ja existente permanece funcional, mas nao e necessario para ler
a janela Diagnostico GATT por HDMI.


### Diagnostico independente do CCCD

O firmware opcional 880de95+diagnostico inclui uma caracteristica de leitura
somente leitura `c24c1e20-9b28-4e07-9c29-20167e20a4e4`. O valor
publico fixo `BMO-GATT-DIAG-1` nao contem chave, numero de serie,
comando nem endereco. Ao abrir o link BLE, antes de `start_notify`,
EasyChange faz exatamente uma leitura GATT sem cache, com timeout.

O log `GATT_LEITURA_TESTE` informa:
- `DIAG_CHARACTERISTIC_ABSENT`: firmware antigo, sem experimento;
- `DIAG_READ_OK`: leitura da caracteristica personalizada permitida,
  podendo o problema estar restrito ao CCCD;
- `DIAG_READ_FAILED:...Access Denied`: leitura do servico personalizado
  tambem foi recusada pelo Windows;
- outro erro: registrar excecao real, sem supor politica corporativa.

O teste nao ignora autorizacao, nao emparelha dispositivo, nao escreve
CCCD, nao executa comandos e nao substitui autenticação SAS/AEAD.


### Firmware Easy: Bluetooth exclusivamente GATT, USB exclusivamente HID

A nova identidade BLE de anunciante e `Easy`, com appearance generic
(0x0000) e UUID do nosso servico 128-bit anunciado. Nenhum servico
Bluetooth HID 0x1812 e exposto nesse firmware. A busca Bleak WinRT
so tenta o nome `Easy`; registro Bluetooth antigo `BT KeyboardMouse E`
e legado e nao pode ser reutilizado como Easy para evitar identidade errada.

O ESP tem duas interfaces fisicas distintas:
- UART USB-serial CH343 (COM4) ligada ao PC **local** controlado pelo MCP;
- USB nativo TinyUSB HID (teclado e mouse) ligado ao PC **remoto**.

O Bluetooth **nao** e um receptor wireless para a porta USB HID. Um
dongle sem fio exigiria um receptor separado no PC remoto. Quando o
USB nativo do ESP nao estiver fisicamente ligado ao PC remoto e
`USB_HID_CONNECTED=0`, nao gravar o novo firmware: os comandos HID
deixarao de funcionar porque nao havera rota BLE de teclado/mouse.

No Windows remoto, `Easy` deve aparecer como dispositivo Bluetooth
LE generico (o Windows determina a rotulagem); emparelhar apenas com
consentimento do operador e sem direitos de administrador. O controle
do EasyChange deve usar o GATT criptografado, mantendo SAS/AEAD.
