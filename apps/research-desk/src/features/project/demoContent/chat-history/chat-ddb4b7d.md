---
created_at: "2026-01-01T13:00:05.000Z"
kind: "inqtrix.chat"
message_order: ["msg-1779029183213-411pz2","msg-1779029183213-tcrk50"]
preview: "Wie kann ich in Python eine Differentialgleichung platten und animieren, gib mir eine Schritt für Schritt Anleitung mit Code."
schema_version: 1
source: "imported"
thread_id: "chat-1779029183213-enhc5n"
title: "Differentialgleichung in Python"
updated_at: "2026-01-01T13:00:10.000Z"
---
<!-- inqtrix:message id="msg-1779029183213-411pz2" role="user" created_at="2026-01-01T13:00:00.000Z" -->
Wie kann ich in Python eine Differentialgleichung platten und animieren, gib mir eine Schritt für Schritt Anleitung mit Code.
<!-- /inqtrix:message -->

<!-- inqtrix:message id="msg-1779029183213-tcrk50" role="assistant" created_at="2026-01-01T13:00:00.000Z" model="claude-sonnet-4-6" model_tier="mid" model_effort="medium" model_requested_tier="mid" model_source="demo" model_effort_source="demo" -->
# Differentialgleichung in Python lösen, plotten & animieren

Hier eine Schritt-für-Schritt-Anleitung am Beispiel eines **gedämpften Pendels**:

$$\ddot{\theta} + b\,\dot{\theta} + \sin(\theta) = 0$$

---

## Schritt 1: Bibliotheken installieren & importieren

```python
# Installation (falls nötig):
# pip install numpy matplotlib scipy

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from scipy.integrate import solve_ivp
```

---

## Schritt 2: DGL als System 1. Ordnung definieren

Jede DGL höherer Ordnung wird in ein System 1. Ordnung umgewandelt:

$$y_0 = \theta, \quad y_1 = \dot{\theta}$$

```python
def pendulum(t, y, b=0.3):
    """Gedämpftes Pendel: θ'' + b*θ' + sin(θ) = 0"""
    theta, omega = y
    dtheta_dt = omega
    domega_dt = -b * omega - np.sin(theta)
    return [dtheta_dt, domega_dt]
```

---

## Schritt 3: DGL numerisch lösen

```python
# Anfangsbedingungen: θ(0) = 2.5 rad, ω(0) = 0
y0 = [2.5, 0.0]

# Zeitspanne
t_span = (0, 20)
t_eval = np.linspace(*t_span, 500)

# Lösung berechnen
sol = solve_ivp(pendulum, t_span, y0, t_eval=t_eval, method='RK45')

theta = sol.y[0]
omega = sol.y[1]
t = sol.t
```

---

## Schritt 4: Statischer Plot

```python
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6))

# Zeitverlauf
ax1.plot(t, theta, label='θ (Winkel)', color='blue')
ax1.plot(t, omega, label='ω (Winkelgeschw.)', color='red')
ax1.set_xlabel('Zeit [s]')
ax1.set_ylabel('Wert')
ax1.legend()
ax1.set_title('Gedämpftes Pendel – Zeitverlauf')
ax1.grid(True)

# Phasenporträt
ax2.plot(theta, omega, color='green')
ax2.set_xlabel('θ')
ax2.set_ylabel('ω')
ax2.set_title('Phasenporträt')
ax2.grid(True)

plt.tight_layout()
plt.savefig('pendel_plot.png', dpi=150)
plt.show()
```

---

## Schritt 5: Animation erstellen

```python
# --- Animation: Pendel als Stab ---
L = 1.0  # Pendellänge

fig_anim, (ax_pend, ax_trace) = plt.subplots(1, 2, figsize=(12, 5))

# Linkes Bild: Pendel-Animation
ax_pend.set_xlim(-1.5, 1.5)
ax_pend.set_ylim(-1.5, 0.5)
ax_pend.set_aspect('equal')
ax_pend.set_title('Pendel')
ax_pend.grid(True)

line, = ax_pend.plot([], [], 'o-', color='darkblue', lw=2, markersize=10)
time_text = ax_pend.text(0.02, 0.95, '', transform=ax_pend.transAxes)

# Rechtes Bild: Zeitverlauf (wachsend)
ax_trace.set_xlim(0, t_span[1])
ax_trace.set_ylim(-3, 3)
ax_trace.set_xlabel('Zeit [s]')
ax_trace.set_ylabel('θ')
ax_trace.set_title('Winkel über Zeit')
ax_trace.grid(True)

trace_line, = ax_trace.plot([], [], color='blue')


def init():
    line.set_data([], [])
    trace_line.set_data([], [])
    time_text.set_text('')
    return line, trace_line, time_text


def animate(i):
    # Pendelposition
    x = [0, L * np.sin(theta[i])]
    y = [0, -L * np.cos(theta[i])]
    line.set_data(x, y)

    # Zeitverlauf aktualisieren
    trace_line.set_data(t[:i+1], theta[:i+1])

    # Zeitanzeige
    time_text.set_text(f't = {t[i]:.1f} s')

    return line, trace_line, time_text


anim = FuncAnimation(
    fig_anim,
    animate,
    init_func=init,
    frames=len(t),
    interval=40,   # 40ms zwischen Frames ≈ 25 FPS
    blit=True
)

# Animation speichern (optional)
# anim.save('pendel.gif', writer='pillow', fps=25)
# anim.save('pendel.mp4', writer='ffmpeg', fps=25)

plt.tight_layout()
plt.show()
```

---

## Zusammenfassung der Schritte

| Schritt | Was passiert | Kernfunktion |
|---------|-------------|--------------|
| 1 | Imports | `numpy`, `matplotlib`, `scipy` |
| 2 | DGL definieren | System 1. Ordnung als Funktion |
| 3 | Numerisch lösen | `solve_ivp()` |
| 4 | Statisch plotten | `plt.plot()` |
| 5 | Animieren | `FuncAnimation()` |

---

## Tipps

- **Andere DGL?** Einfach die Funktion in Schritt 2 anpassen (z. B. Lorenz-System, Van-der-Pol-Oszillator etc.)
- **Steife DGL?** Nutze `method='Radau'` oder `method='BDF'` statt `'RK45'`
- **Interaktiv im Jupyter?** Füge `%matplotlib notebook` oder `%matplotlib widget` am Anfang ein
- **GIF speichern:** `pillow` als Writer, **MP4:** `ffmpeg` muss installiert sein
<!-- /inqtrix:message -->
