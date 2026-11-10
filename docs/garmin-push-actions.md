# Workout su Garmin tramite repo privato e GitHub Actions

Il Personal API di Garmin consente solo letture, e una sessione cloud non ha un login Garmin.
Il percorso alternativo è in due tempi:

1. L'assistente salva obiettivi, piani ed export in un **repository privato** (es.
   `garmin-coach-data`).
2. Il workflow **Push to Garmin** di quel repository, avviato a mano (anche dall'app GitHub sul
   telefono), invia i workout a Garmin con `garmin-coach-push`.

`garmin-coach-push` esegue lo strumento `push_plan_to_garmin` con tutte le sue regole di
sicurezza:
- senza `apply` fa una prova a vuoto, verifica il login Garmin (in sola lettura) ed elenca cosa
  verrebbe creato, programmato o cancellato;
- con `apply` invia solo se `expected_count` ed `expected_delete_count` coincidono con quelli
  della prova a vuoto.

Dopo l'invio, gli id dei workout vengono registrati nel piano e salvati nel repo, così un secondo
invio non crea duplicati. Accade anche se l'invio fallisce a metà.

## Configurazione (una volta)

1. Crea il repo **privato** `garmin-coach-data`, vuoto, e dai accesso all'app Claude GitHub.
2. L'assistente vi copia `templates/garmin-coach-data/` (workflow, `.gitignore`, README) e i
   dati del coach.
3. Nel repo, *Settings → Secrets and variables → Actions*: aggiungi i secret `GARMIN_EMAIL` e
   `GARMIN_PASSWORD`, per un account senza verifica in due passaggi. Mai in chat.
4. Facoltativo: la variabile `GARMIN_COACH_REF` sceglie la versione del server da installare
   (default `main`).

Per le sessioni cloud, aggiungi `garmin-coach-data` ai repository dell'ambiente. Se il repo
viene clonato accanto a questo (`../garmin-coach-data`), `scripts/coach-mcp.sh` lo usa come
cartella dati, così obiettivi e piani restano tra una sessione e l'altra. Dopo ogni modifica,
`scripts/sync-coach-data.sh` fa commit e push dei dati. Lo script rifiuta di salvare file di
token Garmin.

## Uso

1. *Actions → Push to Garmin → Run workflow*, con **apply** non spuntato. Nel riepilogo del job
   compaiono l'esito del login, l'elenco e i conteggi da usare.
2. Controlla l'elenco. Poi *Run workflow* di nuovo con **apply** spuntato e i due conteggi,
   lasciando uguali `plan_id`, `from_date` e `weeks`.
3. Sincronizza l'orologio con Garmin Connect.

## Rischi

- Usa l'accesso **non ufficiale** a Garmin Connect (`docs/api-coverage.md`, rischio R4): Garmin
  può cambiarlo o bloccare i login dai server di GitHub. La prova a vuoto lo rivela prima di
  qualsiasi scrittura.
- Le credenziali stanno nei secret di GitHub, cifrati e non visibili nei log. I token di
  sessione vivono solo nella cartella temporanea del job.
- Il repo contiene dati di allenamento e derivati da dati di salute: **deve restare privato**.

## Check-in settimanale automatico

Il workflow **Weekly coach** (`weekly-coach.yml`) parte ogni lunedì mattina (04:17 UTC) e si può
avviare anche a mano. Usa `garmin-coach-weekly`, che passa dagli strumenti MCP con le stesse
regole di sicurezza:

1. **Revisione** della settimana appena chiusa con `review_week`: sessioni fatte o saltate, lenti
   troppo intensi, segnali di recupero (FC a riposo, HRV, sonno).
2. **Adattamento** con le modifiche proposte dal motore (`suggested_changes`), applicate con
   `apply_plan_update` sulla revisione corrente del piano:
   - con almeno 2 segnali di recupero: settimana successiva −20% e sedute dure facili;
   - dopo una settimana completata sotto il 60%: settimana successiva ridotta.
   Mai più volume. Con l'input `no_adapt` fa solo la revisione.
3. **Livello di prestazione**: se il VDOT attuale si discosta di almeno 1 da quello del piano,
   il riepilogo propone di rigenerare i ritmi con l'assistente.
4. **Settimana su Garmin**: prova a vuoto dell'invio della settimana in corso. Se la variabile
   del repository `GARMIN_AUTO_PUSH` vale `true`, invia esattamente ciò che la prova ha elencato.
   Impostarla è un'**autorizzazione permanente** agli invii settimanali; senza, il riepilogo
   indica i valori per *Push to Garmin*.

Il piano aggiornato e gli id dei workout vengono salvati nel repository. Ogni esecuzione apre
una issue con il riepilogo, notificata da GitHub anche sull'app.

Il piano usato è quello **attivo**: la gara futura più vicina, ultimo piano non sostituito. Una
gara di preparazione già corsa non nasconde quindi l'obiettivo principale. Vale anche per
*Push to Garmin* quando `plan_id` è vuoto.

Secret facoltativo `GARMIN_PERSONAL_API_KEY`: con la chiave, le letture passano dall'API ufficiale
(salute e recupero inclusi). Senza, usano lo stesso login dell'invio.

## Anteprima della domenica

Il workflow **Coach preview** (`coach-preview.yml`) parte ogni domenica sera (18:43 UTC) e si può
avviare anche a mano. Esegue `garmin-coach-weekly --preview`, cioè lo stesso check-in di lunedì
su una **copia** dei dati, con le attività sincronizzate fino a quel momento. Il riepilogo
mostra la revisione della settimana, gli eventuali adattamenti e i workout che lunedì verrebbero
inviati a Garmin. Non modifica nulla: niente commit, niente scritture su Garmin, niente issue.
Lunedì il check-in decide con i dati sincronizzati nel frattempo: una corsa fatta dopo
l'anteprima può ancora cambiare il risultato.

## Report su Telegram

Con i secret `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, sia **Weekly coach** sia **Coach
preview** inviano il riepilogo in chat con `garmin-coach-telegram`. Senza secret il passo non fa
nulla. Un errore di Telegram non fa fallire il check-in.

Il messaggio è una scheda in italiano costruita dai dati del check-in (`garmin-coach-weekly
--digest`): obiettivo e giorni alla gara, sedute della settimana passata (✅ fatta, 🟡 parziale,
❌ saltata, 🔥 troppo intensa), km fatti su pianificati, recupero, adattamenti del piano, variazione
del VDOT, gli allenamenti della settimana con tipo, km, durata e ritmo, e l'esito dell'invio a
Garmin. Sotto c'è il pulsante **📄 Report completo** verso l'esecuzione su GitHub. Se i dati
mancano o non si leggono, viene inviato il report testuale.

1. Su Telegram apri **@BotFather**, invia `/newbot` e segui le istruzioni: ricevi il token del
   bot.
2. Scrivi un messaggio qualsiasi al nuovo bot (o aggiungilo a un gruppo e scrivi lì).
3. Nel browser apri `https://api.telegram.org/bot<TOKEN>/getUpdates`: il numero in
   `"chat":{"id":...}` è il chat id.
4. Nel repo dati, *Settings → Secrets and variables → Actions → Secrets*: aggiungi
   `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Mai in chat.
5. Prova: *Actions → Coach preview → Run workflow*.

Il messaggio contiene il riepilogo (dati di allenamento e di recupero): usa una chat privata
con il bot o un gruppo con persone di fiducia.
