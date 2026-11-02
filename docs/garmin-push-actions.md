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
