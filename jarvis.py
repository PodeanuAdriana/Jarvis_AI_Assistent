import ollama
import pyodbc
import requests
import time
from bs4 import BeautifulSoup
from rich.console import Console
from rich.prompt import Prompt
from ddgs import DDGS
from concurrent.futures import ThreadPoolExecutor
from langdetect import detect as langdetect_detect

console = Console()
MODEL = "qwen2.5:3b"

# --- Conexiune SQL Server ---
conn = pyodbc.connect(
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=DESKTOP-UHMFKS9\SQLEXPRESS;"
    "DATABASE=JarvisDB;"
    "Trusted_Connection=yes;"
)
cursor = conn.cursor()

# --- Creeaza tabelele daca nu exista ---
cursor.execute("""
    IF NOT EXISTS (
        SELECT * FROM sysobjects WHERE name='history' AND xtype='U'
    )
    CREATE TABLE history (
        id INT IDENTITY PRIMARY KEY,
        role NVARCHAR(20),
        content NVARCHAR(MAX),
        model NVARCHAR(100),
        created_at DATETIME DEFAULT GETDATE()
    )
""")
conn.commit()

# --- System prompts ---
SYSTEM_PROMPT = """Esti Jarvis, asistentul personal. Reguli:
- Raspunzi DOAR la ce ti se cere, nimic mai mult
- Maxim 2-3 propozitii per raspuns
- Raspunzi in aceeasi limba in care este scrisa intrebarea (romana sau engleza)
- Fara filosofie, fara divagari, fara texte lungi
- Daca esti salutat, raspunzi scurt: "Buna! Cu ce te pot ajuta?" sau "Hi! How can I help you?"
- Nu repeta intrebarea, nu adauga comentarii inutile"""


SYSTEM_PROMPT_SEARCH = """Esti Jarvis, asistentul personal. Reguli pentru raspunsuri bazate pe internet:
- Raspunzi ELABORAT si DETALIAT folosind informatiile gasite
- Structureaza raspunsul cu paragrafe clare
- Include subiecte conexe relevante
- La final adauga o sectiune cu 2-3 subiecte legate
- Mentioneaza din ce sursa ai luat informatia
- Raspunzi in limba specificata la sfarsitul promptului
- Fara divagari, doar informatii relevante"""

# ✅ Aici, dupa prompturi:
LANG_INSTRUCTIONS = {
    "ro": "IMPORTANT: Raspunde OBLIGATORIU si EXCLUSIV in limba romana. Nicio alta limba nu este acceptata.",
    "en": "IMPORTANT: Reply EXCLUSIVELY in English. No other language is accepted.",
    "fr": "IMPORTANT: Réponds EXCLUSIVEMENT en français. Aucune autre langue n'est acceptée.",
    "de": "IMPORTANT: Antworte AUSSCHLIESSLICH auf Deutsch. Keine andere Sprache wird akzeptiert.",
    "es": "IMPORTANTE: Responde OBLIGATORIA y EXCLUSIVAMENTE en español. No se acepta ningún otro idioma.",
    "it": "IMPORTANTE: Rispondi OBBLIGATORIAMENTE ed ESCLUSIVAMENTE in italiano. Nessun'altra lingua è accettata.",
    "pt": "IMPORTANTE: Responde OBRIGATORIAMENTE e EXCLUSIVAMENTE em português. Nenhuma outra língua é aceite.",
    "nl": "BELANGRIJK: Antwoord VERPLICHT en UITSLUITEND in het Nederlands. Geen enkele andere taal wordt geaccepteerd.",
}

# --- Memorie ---
def get_history():
    cursor.execute("""
        SELECT TOP 6 role, content
        FROM history
        ORDER BY id DESC
    """)
    rows = cursor.fetchall()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]

# def detect_language(text):
#     words = text.lower().split()
    
#     scores = {"ro": 0, "en": 0, "fr": 0, "de": 0}
    
#     indicators = {
#         "ro": ["ce", "cum", "unde", "cand", "care", "este", "sunt", "vreau", 
#                "spune", "cauta", "despre", "mai", "mult", "poti", "imi"],
#         "en": ["the", "what", "how", "why", "is", "are", "can", "tell", 
#                "me", "about", "find", "show", "who", "where", "when", "do"],
#         "fr": ["le", "la", "les", "est", "sont", "que", "qui", "comment", 
#                "pourquoi", "quoi", "cherche", "trouve", "parle", "dis", "moi"],
#         "de": ["der", "die", "das", "ist", "sind", "was", "wie", "warum", 
#                "wo", "wer", "suche", "finde", "erklar", "kannst", "ich"]
#     }
    
#     for word in words:
#         for lang, keywords in indicators.items():
#             if word in keywords:
#                 scores[lang] += 1
    
#     # Returneaza limba cu cel mai mare scor, default romana
#     best_lang = max(scores, key=scores.get)
#     return best_lang if scores[best_lang] > 0 else "ro"

def detect_language(text):
    try:
        return langdetect_detect(text)  # returneaza "ro", "en", "fr", "de" etc automat
    except:
        return "ro"
    
def save_message(role, content, model=None):
    cursor.execute(
        "INSERT INTO history (role, content, model) VALUES (?, ?, ?)",
        role, content, model
    )
    conn.commit()

# --- Search web ---
def fetch_page_content(url, max_chars=1500):
    """Extrage textul dintr-o pagina web pentru context mai bogat."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=5, headers=headers)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return text[:max_chars]
    except Exception:
        return ""

def search_web_fast(query, max_results=3, deep=False):
    """Cauta pe DuckDuckGo. Cu deep=True intra si in paginile gasite."""
    with ThreadPoolExecutor() as executor:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

    enriched = []
    for i, r in enumerate(results):
        entry = {
            "title": r["title"],
            "body": r["body"][:400],
            "url": r["href"]
        }
        # Intra in primele 2 pagini pentru mai mult context
        if deep and i < 2:
            page_content = fetch_page_content(r["href"])
            if page_content:
                entry["body"] = page_content
        enriched.append(entry)

    return enriched

# --- Chat fara search ---
def simple_chat(user_input):
    lang = detect_language(user_input)
    lang_instruction = LANG_INSTRUCTIONS.get(lang, "Raspunde in romana.")
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT + f"\n{lang_instruction}"}] + get_history()
    
    messages.append({"role": "user", "content": user_input})

    response = ""
    console.print("\n[bold cyan]Jarvis:[/bold cyan] ", end="")

    start = time.time()  # ✅ Start timer
    first_token = None   # ✅ Timp pana la primul cuvant


    for chunk in ollama.chat(model=MODEL, messages=messages, stream=True):
        if first_token is None:
            first_token = time.time()  # ✅ Primul token primit
        piece = chunk["message"]["content"]
        response += piece
        console.print(piece, end="", highlight=False)
    end = time.time()  # ✅ Stop timer
    console.print()
     # ✅ Afiseaza statistici
    console.print(f"[dim]⏱ Timp gandire: {first_token - start:.2f}s | Timp total: {end - start:.2f}s[/dim]")
    
    # console.print()
    save_message("assistant", response, MODEL)

# --- Chat cu search ---
def search_chat(user_input, deep=False):
    lang = detect_language(user_input)
    lang_instruction = LANG_INSTRUCTIONS.get(lang, "Raspunde in romana.")
    
    console.print("[yellow]Caut pe internet...[/yellow]")
    start_search = time.time()  # ✅ Start search
    results = search_web_fast(user_input, max_results=3, deep=deep)
    end_search = time.time()    # ✅ End search
    console.print(f"[dim]🔍 Search durată: {end_search - start_search:.2f}s[/dim]")

    if not results:
        console.print("[red]Nu am gasit rezultate. Raspund din cunostinte proprii.[/red]")
        simple_chat(user_input)
        return

    console.print("[green]Am gasit rezultate![/green]")

    search_context = "\n\n".join([
        f"Sursa: {r['title']}\nURL: {r['url']}\nInformatii: {r['body']}"
        for r in results
    ])

    augmented_input = f"""Utilizatorul a intrebat: {user_input}

Am gasit urmatoarele informatii de pe internet:
{search_context}

Raspunde complet si structurat in romana, bazandu-te pe aceste informatii."""
    
    # lang = detect_language(user_input)
    messages = [{"role": "system", "content": SYSTEM_PROMPT_SEARCH + f"\n{lang_instruction}"}] + get_history()
    messages.append({"role": "user", "content": augmented_input})  # ✅ lipsea asta!
   

    response = ""
    console.print("\n[bold cyan]Jarvis:[/bold cyan] ", end="")
    start_ai = time.time()   # ✅ Start AI
    first_token = None
    for chunk in ollama.chat(model=MODEL, messages=messages, stream=True):
        piece = chunk["message"]["content"]
        response += piece
        console.print(piece, end="", highlight=False)
    end_ai = time.time()  # ✅ Stop AI
    console.print()
     # ✅ Afiseaza toate statisticile
    console.print(f"[dim]⏱ Timp gandire AI: {first_token - start_ai:.2f}s | Timp raspuns AI: {end_ai - start_ai:.2f}s | Total: {end_ai - start_search:.2f}s[/dim]")
    
    save_message("assistant", response, MODEL)

    console.print("\n[bold yellow]Surse:[/bold yellow]")
    for i, r in enumerate(results, 1):
        console.print(f"  [cyan]{i}. {r['title']}[/cyan]")
        console.print(f"     {r['url']}")

# --- Router principal ---
def chat(user_input):
    save_message("user", user_input, MODEL)

    search_triggers = [
        "cauta", "search", "gaseste", "găsește",
        "ce stii despre", "informatii despre", "spune-mi despre",
        "news", "noutati", "curiozitati", "ce s-a intamplat",
        "ultimele stiri", "afla", "verifica"
    ]

    # Cauta in profunzime daca utilizatorul cere detalii
    deep_triggers = [
        "detaliat", "detalii", "aprofundat", "mai multe informatii",
        "explica", "cum functioneaza", "tot ce stii"
    ]

    user_lower = user_input.lower()
    needs_search = any(t in user_lower for t in search_triggers)
    needs_deep = any(t in user_lower for t in deep_triggers)

    if needs_search:
        search_chat(user_input, deep=needs_deep)
    else:
        simple_chat(user_input)

# --- Pornire ---
console.print("[bold purple]== JARVIS v0.3 ==[/bold purple]\n")

response = ""
console.print("[bold cyan]Jarvis:[/bold cyan] ", end="")
for chunk in ollama.chat(model=MODEL, stream=True, messages=[
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "Saluta-l pe utilizator scurt si prietenos, max o propozitie."}
]):
    piece = chunk["message"]["content"]
    response += piece
    console.print(piece, end="", highlight=False)
console.print()

while True:
    user_input = Prompt.ask("[bold green]Tu[/bold green]")
    if user_input.lower() in ["sfarsit", "incheiere", "încheiere", "exit", "quit", "pa","bye", "Goodbye"]:
        console.print("[bold cyan]Jarvis:[/bold cyan] La revedere!")
        break
    chat(user_input)