# Teste óptico EC2 com contagem de QR

O contador do EasyChange acompanha QR apresentados nos QLabel (incluindo
EC2H provisórios e páginas retransmitidas). O MCP usa somente o HDMI
canônico já aberto; não usa acesso direto ao filesystem do PC remoto.

Fluxo, depois de atualizar o EasyChange no PC remoto:

1. record_test_easychange(action="arm"): grava PNGs de QR do HDMI na pasta TEMP
   local exclusiva da sessão. Não executa comandos remotos.
2. record_test_easychange(action="start"): inicia contador do EasyChange via
   ESP HID e confirma pelo resultado EC2/HDMI.
3. Teste normalmente: easychange_read_pack, easychange_read_regions,
   easychange_discover_many, buscas e escritas controladas.
4. record_test_easychange(action="pause"): pausa o contador remoto e depois
   o gravador local. Resultado degradado não é tratado como sucesso.
5. record_test_easychange(action="verify",page_budget=8): recupera manifesto
   remoto paginado via HID/HDMI. Repita em VERIFY_IN_PROGRESS. Compara SHA-256
   do texto QR enviado com os QR decodificados das imagens capturadas.
6. record_test_easychange(action="cleanup"): apaga SOMENTE os PNGs da pasta
   da sessão, no PC local. Nunca apaga pastas de terceiros.
7. record_test_easychange(action="status"): estado apenas local, sem HID.

Contagens: qr_display_events = atribuições de QR aos QLabel (inclui repetições);
qr_unique = hashes únicos exibidos; png_images_saved = imagens visuais distintas
capturadas; missing_unique = hashes remotos ausentes dos PNGs decodificáveis.
Essas contagens NÃO são equivalentes a frames HDMI de 60fps.

Limites: amostragem padrão 100 ms (entre 50 e 1000 ms), até 6000 imagens
(configurável até 15000). Jitter e borramento HDMI podem dificultar leitura;
a análise retorna imagens ilegíveis, erros de escrita e saturação. O recorder
não certifica captura literal de todos os 60 quadros/segundo.

Privacidade: os QR podem transportar conteúdo sensível. Os PNGs ficam
exclusivamente em TEMP do PC local; execute cleanup após o teste.
