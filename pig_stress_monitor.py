"""
PigWatch -- Agent de monitoring de stress thermique ET froid des porcs
=====================================================================
Flow complet (bout en bout, un seul run) :
  1. Tracking video (pig_tracking_pipeline.run_pig_tracking) -> Nano Omni donne
     un jugement QUALITATIF de dispersion ("grouped"/"dispersed") pour 2 ticks :
     le premier tick exploitable (baseline) et le tick final (etat courant) --
     ca donne un vrai avant/apres (ex: disperses au debut -> regroupes a la
     fin). Pas de recalcul de coordonnees ici -- Omni a deja fait ce travail,
     on ne le refait pas cote capteurs.
  2. JSON capteurs (temp, humidity, THI) sur la meme fenetre de temps.
  3. Calcul d'un resume compact (tendances capteurs + lecture Omni des 2 ticks).
  4. Ultra raisonne sur les deux types de stress (chaud ET froid).
  5. Rapport 4 lignes structure + dashboard terminal.

Note stress froid (Come Wozniak) :
  T degrees baissante + humidite montante + porcs regroupes = stress FROID
  -> risque d'ecrasement (piling) et problemes respiratoires
  -> NE PAS appliquer les seuils THI chaleur dans ce cas
  -> laisser Ultra raisonner librement sur le contexte complet
"""

import json
import os
import sys
import statistics
import argparse
from pathlib import Path

# Some Windows terminals (e.g. Git Bash pipes) report a legacy cp1252 stdout
# encoding that can't render the degree sign/arrows/emoji this script prints.
# Force UTF-8 regardless of how the script is invoked.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich import box

from pig_tracking_pipeline import run_pig_tracking, CRUSOE_BASE_URL

# ─── CONFIG ──────────────────────────────────────────────────────────────────

# Ultra is served through the same Crusoe inference endpoint as Nano Omni --
# one provider, one API key (CRUSOE_API_KEY) for both calls.
MODEL_ULTRA = "nvidia/NVIDIA-Nemotron-3-Ultra-550B"

TIER_COLORS = {
    "NOMINAL": "green", "WATCH": "yellow",
    "WARNING": "dark_orange", "CRITICAL": "red"
}
TIER_EMOJIS = {
    "NOMINAL": "✅", "WATCH": "⚠️ ",
    "WARNING": "🔶", "CRITICAL": "🚨"
}

# legacy_windows=False: sidesteps a rich bug where its legacy Win32 console
# renderer tries to encode unicode (deg sign, arrows, emoji) through cp1252
# and crashes on non-ANSI-native Windows terminals (e.g. Git Bash pipes).
console = Console(legacy_windows=False)

# ─── 1. ANALYSE CAPTEURS ─────────────────────────────────────────────────────

def analyze_sensors(records: list) -> dict:
    """Extrait les tendances cles depuis le JSON capteurs."""
    temps = [r["temperature_ambient"] for r in records]
    hums  = [r["relative_humidity"]   for r in records]
    this  = [r["calculated_thi"]      for r in records]
    n     = len(records)

    # Tendance = derniere valeur - premiere valeur
    temp_trend = temps[-1] - temps[0]
    hum_trend  = hums[-1]  - hums[0]
    thi_trend  = this[-1]  - this[0]

    return {
        "n":           n,
        "t_start":     records[0]["timestamp"],
        "t_end":       records[-1]["timestamp"],
        "temp_first":  temps[0],
        "temp_last":   temps[-1],
        "temp_mean":   statistics.mean(temps),
        "temp_trend":  temp_trend,   # negatif = descend (cold stress possible)
        "hum_first":   hums[0],
        "hum_last":    hums[-1],
        "hum_mean":    statistics.mean(hums),
        "hum_trend":   hum_trend,    # positif = monte (aggrave stress froid)
        "thi_mean":    statistics.mean(this),
        "thi_last":    this[-1],
        "thi_trend":   thi_trend,
    }

# ─── 2. LECTURE DE LA POSTURE PORCS (depuis Omni, pas de recalcul local) ─────

def describe_omni_tick(tick: dict) -> str:
    """Formate un tick de pig_tracking_pipeline (tick/timestamp_s/pigs/
    nano_omni_analysis) en une ligne lisible. nano_omni_analysis peut contenir
    {"error": ...} si l'appel Omni a echoue -- on le signale au lieu de
    plantage sur des cles manquantes."""
    analysis = tick.get("nano_omni_analysis", {})
    n_pigs = len(tick.get("pigs", {}))
    if "error" in analysis:
        return f"t={tick['timestamp_s']:.1f}s : {n_pigs} porcs -- analyse Omni indisponible ({analysis['error']})"
    return (f"t={tick['timestamp_s']:.1f}s : {n_pigs} porcs, "
            f"disposition={analysis.get('spatial_distribution', 'inconnue')}, "
            f"concern={analysis.get('possible_concern', 'inconnu')} "
            f"-- {analysis.get('clustering_notes', '')}")

# ─── 3. CONSTRUCTION DU RESUME COMPACT ───────────────────────────────────────

def build_summary(sensors: dict, omni_ticks: list, enclosure_id: str = "01") -> str:
    """
    Fusionne tendances capteurs + jugement qualitatif Omni (grouped/dispersed)
    en un resume compact. C'est CE resume (et seulement lui) qui est envoye a
    Ultra -- pas de recalcul de spread depuis des coordonnees brutes, Omni a
    deja fait cette lecture spatiale.
    """
    temp_dir = "en baisse" if sensors["temp_trend"] < -0.05 else (
               "en hausse" if sensors["temp_trend"] > 0.05 else "stable")
    hum_dir  = "en hausse" if sensors["hum_trend"]  > 0.1  else (
               "en baisse" if sensors["hum_trend"]  < -0.1  else "stable")

    omni_lines = "\n".join(f"  - {describe_omni_tick(tk)}" for tk in omni_ticks)

    summary = f"""ENCLOSURE {enclosure_id} — fenetre {sensors['t_start']} → {sensors['t_end']} ({sensors['n']} mesures)

Temperature   : {sensors['temp_first']}°C → {sensors['temp_last']}°C ({temp_dir}, Δ={sensors['temp_trend']:+.2f}°C)
Humidite      : {sensors['hum_first']}% → {sensors['hum_last']}% ({hum_dir}, Δ={sensors['hum_trend']:+.2f}%)
THI (Xin & Harmon 1998) : {sensors['thi_mean']:.2f} (derniere valeur : {sensors['thi_last']:.2f})

Lecture vision (Nano Omni), 2 instants du clip :
{omni_lines}"""

    return summary


# ─── 4. DASHBOARD TERMINAL ───────────────────────────────────────────────────

def render_dashboard(sensors: dict, omni_ticks: list, summary: str):
    console.print()
    console.rule("[bold]PigWatch — Monitoring stress porcs[/bold]", style="dim")
    console.print()

    # Tableau metriques capteurs
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold dim", title="Donnees capteurs")
    t.add_column("Parametre",  style="dim",  width=28)
    t.add_column("Debut",      style="bold", width=12, justify="right")
    t.add_column("Fin",        style="bold", width=12, justify="right")
    t.add_column("Tendance",   style="bold", width=14, justify="right")

    def trend_str(val, invert=False):
        if abs(val) < 0.02:
            return "[dim]→ stable[/dim]"
        up = val > 0
        if invert:
            up = not up
        arrow = "↑" if val > 0 else "↓"
        color = "red" if up else "green"  # hausse temp/hum = rouge, baisse = vert
        return f"[{color}]{arrow} {abs(val):.2f}[/{color}]"

    t.add_row("Temperature (°C)",
              f"{sensors['temp_first']:.2f}",
              f"{sensors['temp_last']:.2f}",
              trend_str(sensors['temp_trend'], invert=True))
    t.add_row("Humidite (%)",
              f"{sensors['hum_first']:.1f}",
              f"{sensors['hum_last']:.1f}",
              trend_str(sensors['hum_trend']))
    t.add_row("THI (Xin & Harmon)",
              f"{sensors['thi_mean']:.2f}",
              f"{sensors['thi_last']:.2f}",
              trend_str(sensors['thi_trend']))
    console.print(t)

    # Tableau vision (Omni)
    t2 = Table(box=box.SIMPLE_HEAVY, header_style="bold dim", title="Donnees vision (Nano Omni)")
    t2.add_column("Tick",             style="dim",  width=10, justify="right")
    t2.add_column("Porcs",            style="bold", width=8,  justify="right")
    t2.add_column("Disposition",      style="bold", width=16)
    t2.add_column("Concern possible", style="bold", width=14)
    t2.add_column("Notes",            style="dim",  width=30)

    concern_colors = {"aucune": "green", "incertain": "yellow",
                       "chaleur": "dark_orange", "froid": "cyan", "stress": "red"}

    for tk in omni_ticks:
        analysis = tk.get("nano_omni_analysis", {})
        n_pigs = len(tk.get("pigs", {}))
        if "error" in analysis:
            t2.add_row(f"{tk['timestamp_s']:.1f}s", str(n_pigs),
                       "[red]indisponible[/red]", "-", f"[red]{analysis['error']}[/red]")
            continue
        concern = analysis.get("possible_concern", "inconnu")
        color = concern_colors.get(concern, "white")
        t2.add_row(f"{tk['timestamp_s']:.1f}s", str(n_pigs),
                   analysis.get("spatial_distribution", "inconnue"),
                   f"[{color}]{concern}[/{color}]",
                   analysis.get("clustering_notes", ""))
    console.print(t2)

    # Resume compact envoye au modele
    console.print(Panel(
        f"[dim]{summary}[/dim]",
        title="[bold]Resume envoye a Nemotron Ultra[/bold]",
        border_style="blue"
    ))
    console.print()


# ─── 5. APPEL NEMOTRON ULTRA ─────────────────────────────────────────────────

def call_ultra(summary: str, api_key: str) -> str:
    """
    Envoie le resume compact a Ultra.
    Le modele repond en exactement 4 lignes structurees.
    Ultra raisonne librement sur stress chaud ET froid selon le contexte.
    """
    client = OpenAI(base_url=CRUSOE_BASE_URL, api_key=api_key)

    console.print(Rule("Analyse Nemotron Ultra 550B", style="dim"))
    console.print()

    system_prompt = """You are a pig welfare monitor. Analyze the enclosure data and reason about BOTH cold stress and heat stress:

- COLD STRESS signs: falling temperature, low THI, pigs huddling/piling together to conserve body heat. Risks: crushing, respiratory issues.
- HEAT STRESS signs: rising temperature and THI above 71, high humidity that limits evaporative cooling. Risks: hyperthermia, heat stroke, reduced feed intake.
- IMPORTANT: pigs can also group/huddle for reasons unrelated to cold (e.g. startled by a loud noise or other stimulus) -- grouping alone does not automatically mean cold stress. If temperature and THI are already high, pigs huddling together is a DANGER SIGN rather than a comfort behavior: piled bodies trap heat and block airflow/evaporative cooling, which raises heat stroke risk further. In that situation, treat the grouping as an aggravating factor for heat stress, not evidence of cold, and prioritize the heat-stress reading.

Reply with EXACTLY these four lines and nothing else:
STATUS: (NOMINAL / WATCH / WARNING / CRITICAL)
WHAT'S HAPPENING: one sentence describing the observed situation
LIKELY CAUSE: one sentence explaining the probable cause
RECOMMENDED ACTION: one concrete, immediate action for the farm operator"""

    response = client.chat.completions.create(
        model=MODEL_ULTRA,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": summary}
        ],
        temperature=0.3,
        max_tokens=600,
        stream=False
    )

    return response.choices[0].message.content


# ─── 6. AFFICHAGE DU RAPPORT 4 LIGNES ────────────────────────────────────────

def render_report(report: str):
    """Affiche le rapport Ultra avec couleurs selon le STATUS."""
    lines = report.strip().split("\n")

    # Extraire le STATUS pour la couleur
    tier = "NOMINAL"
    for line in lines:
        if line.startswith("STATUS:"):
            for candidate in ["CRITICAL", "WARNING", "WATCH", "NOMINAL"]:
                if candidate in line.upper():
                    tier = candidate
                    break

    color = TIER_COLORS.get(tier, "white")
    emoji = TIER_EMOJIS.get(tier, "")

    console.print(Panel(
        "\n".join(
            f"[bold {color}]{line}[/bold {color}]" if line.startswith("STATUS:")
            else f"[bold]{line.split(':')[0]}:[/bold]{':'.join(line.split(':')[1:])}"
            if ":" in line else line
            for line in lines
        ),
        title=f"{emoji} Rapport Nemotron Ultra",
        border_style=color
    ))
    console.print()


# ─── 7. MAIN ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="Path to top-down pig pen video")
    parser.add_argument("--sensors", required=True,
                         help="Path to sensor JSON: list of records with timestamp, "
                              "temperature_ambient, relative_humidity, calculated_thi")
    parser.add_argument("--interval", type=float, default=1.0,
                         help="Seconds between internally tracked video samples")
    parser.add_argument("--pen-width-m", type=float, default=6.0)
    parser.add_argument("--pen-height-m", type=float, default=4.0)
    parser.add_argument("--enclosure-id", default="01")
    parser.add_argument("--save-frames-dir", default=None,
                         help="Optional dir to save each rendered PNG for inspection/demo")
    args = parser.parse_args()

    crusoe_key = os.environ.get("CRUSOE_API_KEY")
    if not crusoe_key:
        sys.exit("Set CRUSOE_API_KEY as an environment variable before running this script.")

    sensors_path = Path(args.sensors)
    if not sensors_path.exists():
        console.print(f"[red]Fichier capteurs introuvable : {args.sensors}[/red]")
        sys.exit(1)

    with open(sensors_path) as f:
        records = json.load(f)

    console.print()
    console.print(Panel(
        f"[bold]PigWatch[/bold] — Monitoring stress thermique porcs\n"
        f"[dim]{Path(args.video).name} + {sensors_path.name} | {len(records)} mesures capteurs[/dim]",
        border_style="blue"
    ))

    # Tracking video + lecture Omni (2 ticks : baseline + etat final)
    crusoe_client = OpenAI(base_url=CRUSOE_BASE_URL, api_key=crusoe_key)
    omni_ticks = run_pig_tracking(args.video, crusoe_client, interval=args.interval,
                                   pen_width_m=args.pen_width_m, pen_height_m=args.pen_height_m,
                                   save_frames_dir=args.save_frames_dir, log=console.print)

    # Analyse capteurs
    sensors = analyze_sensors(records)

    # Resume compact (fusion capteurs + Omni)
    summary = build_summary(sensors, omni_ticks, enclosure_id=args.enclosure_id)

    # Dashboard
    render_dashboard(sensors, omni_ticks, summary)

    # Appel Ultra + affichage rapport
    try:
        report = call_ultra(summary, crusoe_key)
        render_report(report)
    except Exception as e:
        console.print(f"[red]Erreur API Ultra (Crusoe) : {e}[/red]")

    # Resume final terminal
    console.print(Rule("Resume execution", style="dim"))
    last_tick = omni_ticks[-1]
    last_analysis = last_tick.get("nano_omni_analysis", {})
    console.print(
        f"\n  THI moyen : [bold]{sensors['thi_mean']:.2f}[/bold]  |  "
        f"T° : [bold]{sensors['temp_first']:.2f}→{sensors['temp_last']:.2f}°C[/bold]  |  "
        f"Disposition (dernier tick) : [bold]{last_analysis.get('spatial_distribution', 'inconnue')}[/bold]  |  "
        f"Porcs : [bold]{len(last_tick.get('pigs', {}))}[/bold]\n"
    )


if __name__ == "__main__":
    main()
