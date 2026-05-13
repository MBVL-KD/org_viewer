# Deploy org_viewer (Streamlit)

De code staat op: **https://github.com/MBVL-KD/org_viewer**

## 1. GitHub (al gedaan als `main` op origin staat)

```bash
cd /Users/maartenvanleenen/Desktop/Draughts4All/Clubs
git push -u origin main
```

## 2. Streamlit Community Cloud

1. Ga naar [share.streamlit.io](https://share.streamlit.io) en log in met GitHub.
2. **New app** → kies repo **`MBVL-KD/org_viewer`**, branch **`main`**, main file **`viewer.py`**.
3. **Secrets** (rechtsboven in de app → *Edit secrets*), bijvoorbeeld:

```toml
MONGO_URI = "mongodb+srv://USER:PASS@cluster.xxxxx.mongodb.net/?retryWrites=true&w=majority"
MONGO_DB = "damclubs"
```

Gebruik in de URI een database-user met alleen lees- (en eventueel beperkte schrijf-)rechten op `clubs` en `schools`.

4. **MongoDB Atlas** (als je Atlas gebruikt): onder *Network Access* → **allow** Streamlit’s egress IPs, of tijdelijk `0.0.0.0/0` alleen voor test (niet aanbevolen productie).

5. **Deploy**; bij fouten in de app: *Manage app* → *Logs*.

## 3. Lokaal draaien (zelfde repo)

```bash
pip install -r requirements.txt
export MONGO_URI="..."   # of .env naast viewer.py
export MONGO_DB=damclubs
streamlit run viewer.py
```
