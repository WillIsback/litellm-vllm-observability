# litellm-vllm-observability

Observabilité d'une infra LLM self-hosted : **LiteLLM** (gateway) + **vLLM** (moteurs), avec Prometheus, Grafana
et un générateur de rapport mensuel. Conçu pour 2 stacks vLLM sur NVIDIA GB10, adaptable à toute topologie.

Stack Docker pour **capturer, visualiser et produire un rapport mensuel** sur l'usage de l'infra LLM
mise à disposition de l'équipe dev (accès via la gateway LiteLLM, teams → users → clés API).

```
             ┌──────────── équipe dev (clés API) ────────────┐
                                   │
                           LiteLLM gateway ──── Postgres (LiteLLM_SpendLogs)
                           /metrics/  │                 │
                     ┌────────────────┴───────┐         │
               vLLM stack-a (master)   vLLM stack-b (master)
                   /metrics                /metrics     │
                     └──────────┬─────────────┘         │
                           Prometheus (45 j) ───────────┤
                                │                       │
                     Grafana (3 dashboards) ◄───────────┘
                     reporter (HTML + CSV)  ◄───────────┘
```

Trois sources complémentaires :

| Source | Ce qu'elle dit | Utilisée par |
|---|---|---|
| LiteLLM `/metrics/` | vue « client » : requêtes, tokens, TTFT, latence **par team / user / clé**, santé des déploiements | dashboard *LiteLLM — Usage*, rapport §1 |
| vLLM `/metrics` | vue « moteur » : running/waiting, KV cache, préemptions, prefix cache, TTFT/ITL/file | dashboard *vLLM — Moteurs*, rapport §2 + diagnostic |
| Postgres `LiteLLM_SpendLogs` | une ligne par requête : valeurs **exactes** (percentiles, taille des prompts, concurrence réelle) | dashboard *SpendLogs*, rapport §3 |

## 1. Prérequis côté infra

**LiteLLM** : activer le callback Prometheus (voir `litellm/config-snippet.yaml`) :

```yaml
litellm_settings:
  callbacks: ["prometheus"]
```

Selon ta version de LiteLLM, certaines métriques Prometheus peuvent être réservées à l'édition Enterprise :
la commande `check` (§3) te dira ce qui est réellement exposé. Ne pas activer `disable_spend_logs`, et si une
purge des SpendLogs est configurée, garder une rétention ≥ 1 mois.

**vLLM** : rien à faire, `/metrics` est exposé par défaut (ne pas lancer avec `--disable-log-stats`).
Seul le **master** de chaque stack expose l'API et `/metrics` : c'est lui qu'on scrape.

**Postgres LiteLLM** : créer un rôle en lecture seule, limité aux colonnes utiles :

```sql
CREATE ROLE grafana_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE litellm TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON "LiteLLM_SpendLogs" TO grafana_ro;
GRANT SELECT (team_id, team_alias)              ON "LiteLLM_TeamTable"         TO grafana_ro;
GRANT SELECT (user_id, user_email)              ON "LiteLLM_UserTable"         TO grafana_ro;
GRANT SELECT (token, key_alias, team_id, user_id) ON "LiteLLM_VerificationToken" TO grafana_ro;
```

(`token` est le hash de la clé, pas la clé en clair. Le contenu des prompts n'est pas stocké dans SpendLogs
tant que `store_prompts_in_spend_logs` reste à `false`.)

## 2. Configuration

```bash
cp .env.example .env            # mots de passe, accès Postgres, SLO, HOST_UID/GID (commande `id`)
```

Éditer les cibles :

- `prometheus/targets/litellm.yml` : adresse du proxy LiteLLM.
- `prometheus/targets/vllm.yml` : adresse du master de chaque stack, avec les labels
  `provider` (nom lisible) et `api_base` **identique** à celui déclaré dans le `config.yaml` LiteLLM.
  C'est ce label qui permet de croiser vue gateway et vue moteur.

Les fichiers de cibles sont rechargés à chaud par Prometheus.

## 3. Démarrage et vérification

```bash
docker compose up -d
docker compose run --rm reporter check
```

`check` affiche l'état des cibles (UP/DOWN), les métriques trouvées pour chaque indicateur
(les noms changent selon les versions de vLLM et LiteLLM : les requêtes utilisent des regex tolérantes)
et un échantillon des combinaisons team / user / clé vues par Prometheus.

Grafana : http://localhost:3000, dossier **« LLM - Équipe dev »** :

- **LiteLLM — Usage équipe dev** : filtres Team / Utilisateur / Clé ; KPI de la période, charge et latence par team,
  tableau par utilisateur et clé, santé des déploiements (latence/token, échecs, cooldowns).
- **vLLM — Moteurs** : filtre Stack ; synthèse saturation (pics, % du temps avec file d'attente, % du temps KV > 90 %,
  préemptions, hit rate prefix cache), puis courbes scheduler / mémoire / latences.
- **LiteLLM — SpendLogs** : tableaux exacts par team et par utilisateur, TTFT p95 par stack et par jour,
  concurrence réelle reconstituée.

Les dashboards s'ouvrent sur les 30 derniers jours ; le sélecteur de temps de Grafana pilote toutes les agrégations.

## 4. Rapport mensuel

```bash
docker compose run --rm reporter                     # 30 derniers jours
docker compose run --rm reporter --previous-month    # mois calendaire précédent
docker compose run --rm reporter --start 2026-11-01 --end 2026-12-01
```

Produit dans `./reports/` :

- `rapport-llm_<période>.html` : fichier autonome (graphiques intégrés, fonctionne hors ligne, imprimable en PDF) ;
- `csv_<période>/` : les tableaux en CSV (séparateur `;`, décimale `,`, s'ouvre directement dans Excel/LibreOffice).

Contenu : KPI, **diagnostic automatique**, usage par team / utilisateur / clé, profil jour × heure,
indicateurs moteur par stack, données exactes SpendLogs (TTFT, temps par token, taille des prompts, répartition entre stacks,
concurrence max réelle).

Le diagnostic applique les règles de décision « tuning vLLM ou restriction API » :

| Signal | Lecture | Piste |
|---|---|---|
| file d'attente fréquente, KV cache modéré | le scheduler limite | ↑ `--max-num-seqs` / `--max-num-batched-tokens` |
| KV > 90 % et/ou préemptions | limité par la mémoire | ↓ `--max-model-len`, KV cache FP8, plafonner la concurrence |
| TTFT p95 > SLO, ITL correct | goulot au prefill | chunked prefill, prefix caching, limiter les gros prompts |
| ITL p95 > SLO | batch de décodage trop gros | ↓ `--max-num-seqs` ou plafonner la concurrence |
| un utilisateur concentre les tokens + saturation | contention | limite par clé dans LiteLLM (`max_parallel_requests`, `tpm_limit`) |
| hit rate prefix cache faible | perte d'affinité | revoir la stratégie de routage LiteLLM |
| répartition > 65 % sur une stack | déséquilibre | revoir le routage |

Les seuils SLO se règlent dans `.env` (`SLO_TTFT_P95`, `SLO_ITL_P95`).

Automatisation (cron de l'hôte, le 1er de chaque mois à 7 h) :

```cron
0 7 1 * * cd /opt/llm-obs && docker compose run --rm reporter --previous-month >> reports/cron.log 2>&1
```

## 5. Points d'attention

- **Données nominatives.** Le rapport détaille l'usage par utilisateur : informer l'équipe et, dans une
  organisation soumise au RGPD, vérifier avec le DPO. `ANONYMIZE_USERS=true` remplace les identifiants et les alias de clé
  par `dev-NN` / `clé-NN` dans le rapport (les dashboards Grafana, eux, restent nominatifs).
- **Rétention.** Prometheus garde 45 jours (`PROM_RETENTION`). Pour garder l'historique au-delà, archiver les rapports
  et CSV, ou s'appuyer sur SpendLogs.
- **TTFT gateway** : émis par LiteLLM uniquement pour les requêtes streamées (c'est le cas d'usage majoritaire des outils dev).
- **Précision des percentiles Prometheus** : ils sont interpolés à partir des buckets d'histogramme ; les valeurs exactes
  sont dans la section SpendLogs.
- **Mesure de capacité** : ces données décrivent l'usage réel. Pour connaître le point de rupture de l'infra,
  il faut un test de charge (`vllm bench serve`, GuidelLLM) à travers la gateway ; ces dashboards servent alors à
  observer le test.
- **Option système** : décommenter le job `node` dans `prometheus/prometheus.yml` et copier
  `targets/nodes.yml.example` pour ajouter les métriques des 4 nœuds GB10 (node_exporter).

## Licence

MIT, voir `LICENSE`.

## Arborescence

```
docker-compose.yml
.env.example
prometheus/prometheus.yml           scrape LiteLLM + vLLM
prometheus/targets/*.yml            cibles (rechargées à chaud)
grafana/provisioning/               datasources Prometheus + Postgres, provider de dashboards
grafana/dashboards/*.json           3 dashboards
reporter/                           image du générateur de rapport (Python)
litellm/config-snippet.yaml         réglages à reporter dans LiteLLM
reports/                            sortie des rapports
```
