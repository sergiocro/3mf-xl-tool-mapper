# 3MF XL Tool Mapper

Lokalna aplikacija koja mijenja isključivo `value` atribut zapisa `metadata key="extruder"` u `Metadata/model_settings.config`. Original se nikad ne prepisuje, a izlaz se ponovno otvara i validira.

## Pokretanje

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Aplikacija otvara `http://127.0.0.1:8765`.

## Testovi

```powershell
python -m unittest discover -s tests -v
```
