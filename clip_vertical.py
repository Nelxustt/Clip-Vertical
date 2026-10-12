"""
Clip Vertical - herramienta personal tipo Crossclip, con selector visual.
Enlace o archivo -> MP4 9:16 (centrado con fondo difuminado, o pantalla dividida).
En pantalla dividida arrastras las cajas verde (juego) y naranja (camara)
sobre el video de entrada, y ves el resultado en vivo a la derecha.

Requisitos:
    pip install flask yt-dlp
    pip install faster-whisper   (solo para subtítulos automáticos)
    FFmpeg (incluye ffprobe) instalado y en el PATH (Windows: winget install ffmpeg)
Uso:
    python clip_vertical.py   (abre http://127.0.0.1:5000)
"""
import re
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).parent.resolve()
TRABAJO, SALIDAS = BASE / "trabajo", BASE / "salidas"
for d in (TRABAJO, SALIDAS):
    d.mkdir(exist_ok=True)
app = Flask(__name__)
JOBS = {}
ULTIMO = None


def limpiar(horas: int = 24, excepto: str = None):
    """Borra descargas temporales antiguas (los videos de 'salidas' no se tocan)."""
    limite = time.time() - horas * 3600
    for c in TRABAJO.iterdir():
        if c.is_dir() and c.name != excepto and c.stat().st_mtime < limite:
            shutil.rmtree(c, ignore_errors=True)


def fuente(vid: str) -> Path:
    if not vid.isalnum():
        abort(404)
    f = list((TRABAJO / vid).glob("fuente.*"))
    if not f:
        abort(404)
    return f[0]


def duracion(path: Path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def recorte(r, alto: int) -> str:
    x, y, w, h = [float(v) for v in r]
    return (f"crop=floor(iw*{w}/2)*2:floor(ih*{h}/2)*2:iw*{x}:ih*{y},"
            f"scale=1080:{alto}:force_original_aspect_ratio=increase,crop=1080:{alto}")


def filtro_dividido(juego, camara, pct: float) -> str:
    aj = int(round(1920 * pct / 100 / 2)) * 2
    return (f"[0:v]split=2[a][b];[a]{recorte(juego, aj)}[game];"
            f"[b]{recorte(camara, 1920 - aj)}[cam];[cam][game]vstack[v]")


def filtro_centrado(desfoque: int) -> str:
    return ("[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,boxblur={desfoque}:5[bg2];"
            "[fg]scale=1080:-2[fg2];[bg2][fg2]overlay=(W-w)/2:(H-h)/2[v]")


_modelo = None


def transcribir(wav: Path, idioma: str, avance=None):
    """Devuelve [(inicio, fin, palabra)] con tiempos relativos al audio."""
    global _modelo
    from faster_whisper import WhisperModel
    if _modelo is None:
        _modelo = WhisperModel("small", device="auto", compute_type="auto")
    # Se lee el WAV (16 kHz, mono) aquí mismo y se pasa como arreglo: así no depende
    # del decodificador interno de faster-whisper (que falla con versiones viejas de PyAV).
    import wave

    import numpy as np
    with wave.open(str(wav), "rb") as f:
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segmentos, _ = _modelo.transcribe(audio, language=idioma or None,
                                      word_timestamps=True, vad_filter=True)
    palabras = []
    for sg in segmentos:
        palabras += [(w.start, w.end, w.word.strip()) for w in (sg.words or []) if w.word.strip()]
        if avance:
            avance(sg.end)
    return palabras


def _t(seg: float) -> str:
    cs = int(round(seg * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def agrupar(palabras):
    """Agrupa palabras en bloques de hasta 3 palabras / 18 letras."""
    bloques, b = [], []
    for p in palabras:
        largo = sum(len(x[2]) for x in b) + len(p[2])
        if b and (len(b) >= 3 or largo > 18 or p[0] - b[-1][1] > 0.6):
            bloques.append(b)
            b = []
        b.append(p)
        if p[2][-1] in ".?!,;:":
            bloques.append(b)
            b = []
    if b:
        bloques.append(b)
    return bloques


def crear_ass(bloques, destino: Path):
    """Un evento por palabra: muestra el bloque completo con la palabra activa resaltada."""
    lineas = []
    for b in bloques:
        pal = [(a, c, re.sub(r"[{}\\]", "", str(t))) for a, c, t in b]
        for i, (ini, fin, _) in enumerate(pal):
            fin = pal[i + 1][0] if i + 1 < len(pal) else fin
            texto = " ".join(
                ("{\\c&H003FD2FF&}%s{\\c&H00FFFFFF&}" % w) if j == i else w
                for j, (_, _, w) in enumerate(pal))
            lineas.append(f"Dialogue: 0,{_t(ini)},{_t(max(fin, ini + 0.05))},Sub,,0,0,0,,{texto}")
    cabecera = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
        "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Sub,Arial,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,7,0,2,60,60,520,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    destino.write_text(cabecera + "\n".join(lineas) + "\n", encoding="utf-8")


def total_de(src: Path, ini: float, fin: float) -> float:
    return (fin - ini) if fin > ini else max((duracion(src) or 1) - ini, 0.1)


def palabras_de(job, src: Path, ini: float, fin: float, idioma: str):
    """Extrae el audio del tramo y lo transcribe. Tiempos relativos a 'ini'."""
    total = total_de(src, ini, fin)
    job.update(etapa="Extrayendo audio", pct=None)
    wav = src.parent / "audio.wav"
    ca = ["ffmpeg", "-y"] + (["-ss", str(ini)] if ini > 0 else []) + ["-i", str(src)]
    ca += (["-t", str(fin - ini)] if fin > ini else []) + ["-vn", "-ac", "1", "-ar", "16000", str(wav)]
    ra = subprocess.run(ca, capture_output=True, text=True)
    if ra.returncode != 0:
        raise RuntimeError("No se pudo extraer el audio:\n" + ra.stderr[-400:])
    job.update(etapa="Transcribiendo", pct=0)

    def avance(t):
        if job["cancel"]:
            raise InterruptedError
        job["pct"] = min(99, t / total * 100)

    return transcribir(wav, idioma, avance)


def generar_subs(job, src: Path, ini: float, fin: float, idioma: str):
    """Solo transcribe y devuelve los bloques (tiempos absolutos del video) para editarlos."""
    bloques = [[[round(a + ini, 2), round(c + ini, 2), t] for a, c, t in bl]
               for bl in agrupar(palabras_de(job, src, ini, fin, idioma))]
    job.update(estado="listo", pct=100, bloques=bloques)


def ejecutar(job, d: dict, src: Path, filtro: str, ini: float, fin: float):
    carpeta = src.parent
    total = total_de(src, ini, fin)
    mapa, aviso = "[v]", None
    bloques = d.get("bloques")  # ya editados por el usuario (tiempos relativos al inicio)
    if bloques is None and d.get("subs"):
        bloques = agrupar(palabras_de(job, src, ini, fin, d.get("idioma", "")))
        if not bloques:
            aviso = "No se detectó voz: el clip salió sin subtítulos."
    if bloques:
        crear_ass(bloques, carpeta / "subs.ass")
        filtro += ";[v]ass=subs.ass[vs]"
        mapa = "[vs]"

    nombre = f"clip_{uuid.uuid4().hex[:8]}.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if ini > 0:
        cmd += ["-ss", str(ini)]
    cmd += ["-i", str(src)]
    if fin > ini:
        cmd += ["-t", str(fin - ini)]
    cmd += ["-filter_complex", filtro, "-map", mapa, "-map", "0:a?",
            "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(SALIDAS / nombre)]
    job.update(etapa="Codificando video", pct=0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=carpeta)
    job["proc"] = proc
    cola = []
    for linea in proc.stdout:
        if job["cancel"]:
            proc.terminate()
        if linea.startswith("out_time_us="):
            try:
                job["pct"] = min(99, int(linea.split("=")[1]) / 1e6 / total * 100)
            except ValueError:
                pass
        elif not re.match(r"^\w+=", linea):
            cola = (cola + [linea])[-12:]
    proc.wait()
    if job["cancel"]:
        raise InterruptedError
    if proc.returncode != 0:
        raise RuntimeError("FFmpeg falló:\n" + "".join(cola)[-500:])
    job.update(estado="listo", pct=100, url="/salidas/" + nombre, aviso=aviso)


def en_hilo(jid: str, fn, *args):
    job = JOBS[jid]
    try:
        fn(job, *args)
    except InterruptedError:
        job.update(estado="error", error="Cancelado.")
    except ImportError:
        job.update(estado="error", error="Falta faster-whisper. Instálalo con: pip install faster-whisper")
    except Exception as e:
        job.update(estado="error", error=str(e)[-600:])


@app.get("/")
def index():
    return HTML


@app.post("/cargar")
def cargar():
    global ULTIMO
    vid = uuid.uuid4().hex
    carpeta = TRABAJO / vid
    carpeta.mkdir()
    url = (request.form.get("url") or "").strip()
    try:
        if url:
            opc = {"format": "bv*[height<=1080]+ba/b[height<=1080]/b",
                   "merge_output_format": "mp4", "noplaylist": True, "quiet": True,
                   "outtmpl": str(carpeta / "fuente.%(ext)s")}
            with yt_dlp.YoutubeDL(opc) as y:
                y.download([url])
        elif "archivo" in request.files:
            a = request.files["archivo"]
            a.save(carpeta / ("fuente" + Path(a.filename).suffix))
        else:
            shutil.rmtree(carpeta, ignore_errors=True)
            return jsonify(error="Pega un enlace o sube un video."), 400
    except Exception as e:
        shutil.rmtree(carpeta, ignore_errors=True)
        return jsonify(error=str(e)[-300:]), 500
    if ULTIMO:
        shutil.rmtree(TRABAJO / ULTIMO, ignore_errors=True)
    ULTIMO = vid
    limpiar(excepto=vid)
    return jsonify(id=vid)


@app.get("/video/<vid>")
def video(vid):
    return send_file(fuente(vid), conditional=True)


@app.get("/salidas/<nombre>")
def salida(nombre):
    return send_from_directory(SALIDAS, nombre)


@app.post("/compilar")
def compilar():
    d = request.get_json()
    src = fuente(d["id"])
    ini, fin = float(d.get("inicio") or 0), float(d.get("fin") or 0)
    try:
        if d["modo"] == "centrado":
            filtro = filtro_centrado(int(d.get("desfoque", 25)))
        else:
            filtro = filtro_dividido(d["juego"], d["camara"], float(d["pct"]))
    except (KeyError, ValueError):
        return jsonify(error="Regiones inválidas."), 400
    jid = uuid.uuid4().hex
    JOBS[jid] = {"estado": "corriendo", "etapa": "Preparando", "pct": None, "cancel": False, "proc": None}
    threading.Thread(target=en_hilo, args=(jid, ejecutar, d, src, filtro, ini, fin), daemon=True).start()
    return jsonify(job=jid)


@app.post("/subtitulos")
def subtitulos():
    d = request.get_json()
    src = fuente(d["id"])
    ini, fin = float(d.get("inicio") or 0), float(d.get("fin") or 0)
    jid = uuid.uuid4().hex
    JOBS[jid] = {"estado": "corriendo", "etapa": "Preparando", "pct": None, "cancel": False, "proc": None}
    threading.Thread(target=en_hilo, args=(jid, generar_subs, src, ini, fin, d.get("idioma", "")),
                     daemon=True).start()
    return jsonify(job=jid)


@app.get("/progreso/<jid>")
def progreso(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(estado="error", error="Trabajo no encontrado."), 404
    return jsonify({k: v for k, v in j.items() if k not in ("proc", "cancel")})


@app.post("/cancelar/<jid>")
def cancelar(jid):
    j = JOBS.get(jid)
    if j:
        j["cancel"] = True
        if j["proc"]:
            j["proc"].terminate()
    return jsonify(ok=True)


HTML = r"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clip Vertical</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%20120%20120%27%3E%3Crect%20x%3D%274%27%20y%3D%2728.5%27%20width%3D%27112%27%20height%3D%2763%27%20rx%3D%279%27%20fill%3D%27none%27%20stroke%3D%27%235cc8ff%27%20stroke-width%3D%273%27%20stroke-opacity%3D%27.4%27%2F%3E%3Crect%20x%3D%2730.75%27%20y%3D%278%27%20width%3D%2758.5%27%20height%3D%27104%27%20rx%3D%2711%27%20fill%3D%27%235cc8ff%27%2F%3E%3Cpath%20d%3D%27M53%2048L53%2072L73%2060Z%27%20fill%3D%27%230f1720%27%20stroke%3D%27%230f1720%27%20stroke-width%3D%274%27%20stroke-linejoin%3D%27round%27%2F%3E%3C%2Fsvg%3E">
<style>
:root{--bg:#0f1720;--pn:#16212c;--ln:#27384a;--tx:#e8eef3;--mu:#8da0b1;--ac:#5cc8ff;--g:#4ade80;--o:#fb923c;color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 Bahnschrift,"DIN Alternate","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;font-variant-numeric:tabular-nums;height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
header{flex:none;display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:10px 20px;border-bottom:1px solid var(--ln)}
h1{margin:0;font-size:18px;font-weight:600;display:flex;align-items:center;gap:10px}
h1 svg{width:32px;height:32px;flex:none}
h2{margin:0;font-size:15px;font-weight:600}
.src{display:flex;gap:8px;flex:1;min-width:280px;align-items:center;flex-wrap:wrap}
#url{flex:1;min-width:180px}
#st{color:var(--mu);font-size:13px}
input:not([type=range]):not([type=file]),select,button{font:inherit;color:var(--tx);background:var(--pn);border:1px solid var(--ln);border-radius:6px;padding:7px 11px}
input::placeholder{color:var(--mu)}
button{cursor:pointer}button:hover{border-color:var(--mu)}
:is(input,select,button):focus-visible{outline:2px solid var(--ac);outline-offset:2px}
input[type=file]{color:var(--mu);font-size:13px;max-width:220px}
input[type=file]::file-selector-button{font:inherit;color:var(--tx);background:var(--pn);border:1px solid var(--ln);border-radius:6px;padding:7px 11px;margin-right:10px;cursor:pointer}
.p{background:var(--ac);border-color:var(--ac);color:#06202e;font-weight:600}
.p:hover{background:#8dd9ff;border-color:#8dd9ff}
input[type=range]{-webkit-appearance:none;appearance:none;height:4px;border-radius:4px;background:var(--ln);outline:0}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--ac);border:0}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--ac);border:0}
input[type=range]:focus-visible{outline:2px solid var(--ac);outline-offset:6px}
main{flex:1;min-height:0;width:100%;max-width:1600px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr) clamp(260px,26vw,340px);grid-template-rows:minmax(0,1fr)}
.work{display:flex;flex-direction:column;gap:10px;min-width:0;min-height:0;padding:14px 20px}
.head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.hint,.cap{color:var(--mu);font-size:13px}
.stage{position:relative;flex:1;min-height:0;background:#000;border-radius:4px;overflow:hidden;display:grid;place-items:center}
#cin{display:block;cursor:grab}
#vacio{position:absolute;inset:0;display:grid;place-items:center;padding:24px;text-align:center;color:var(--mu)}
#vacio[hidden]{display:none}
.tp{flex:none;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ic{width:36px;height:36px;padding:0;border-radius:50%;display:grid;place-items:center}
#t{color:var(--mu);min-width:112px}
#lab{color:var(--mu)}
.sp{flex:1}
#tl{flex:none;position:relative;height:56px;border-radius:6px;overflow:hidden;background:#000;touch-action:none;cursor:pointer;user-select:none}
#film{position:absolute;inset:0;width:100%;height:100%}
#sel{position:absolute;top:0;bottom:0;border-top:3px solid var(--ac);border-bottom:3px solid var(--ac);box-shadow:0 0 0 100vmax rgba(15,23,32,.68);pointer-events:none}
.hd{position:absolute;top:0;bottom:0;width:12px;background:var(--ac);cursor:ew-resize;z-index:2}
.hd::after{content:"";position:absolute;left:50%;top:50%;width:2px;height:16px;margin:-8px 0 0 -1px;background:#06202e;border-radius:1px}
#ph{position:absolute;top:0;bottom:0;width:2px;background:#fff;pointer-events:none;z-index:3;box-shadow:0 0 0 1px rgba(0,0,0,.5)}
.insp{display:flex;flex-direction:column;gap:12px;min-height:0;padding:14px 18px;border-left:1px solid var(--ln);overflow:auto}
.insp section{flex:none;padding-bottom:12px;border-bottom:1px solid var(--ln)}
.insp section:last-of-type{border:0;padding:0}
.insp section.sal{flex:1;min-height:120px;display:flex;flex-direction:column;gap:8px}
.pbox{flex:1;min-height:0;display:grid;place-items:center;overflow:hidden}
.phone{padding:6px;border-radius:22px;background:#05080c;border:1px solid var(--ln);box-shadow:0 18px 40px -20px rgba(92,200,255,.35)}
#cout{display:block;border-radius:16px}
.fld{display:block}.fld+.fld{margin-top:10px}
.fh{display:flex;justify-content:space-between;color:var(--mu);margin-bottom:5px}
.fld select,.fld input[type=range]{width:100%}
.key{display:flex;gap:16px;margin-top:10px;color:var(--mu);font-size:13px}
.key span{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.row{display:flex;gap:8px;margin-top:8px}.row>*{min-width:0}.row input{flex:1}.row select{width:100%}
.big{width:100%;padding:12px;font-size:15px;flex:none}
#res:empty{display:none}
#res:not(:empty){position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:20;width:min(92vw,400px);max-height:92dvh;overflow:auto;background:var(--pn);border:1px solid var(--ln);border-radius:12px;padding:14px;box-shadow:0 0 0 100vmax rgba(5,8,12,.65)}
#res video{width:100%;height:auto;max-height:66dvh;border-radius:6px;background:#000;display:block;margin:10px 0}
#res pre{white-space:pre-wrap;color:#ff8a80;font:12px/1.4 ui-monospace,Consolas,monospace;margin:10px 0 0}
#res a{color:var(--ac)}
.x{float:right}
.chk{display:flex;align-items:center;gap:8px;flex:none;white-space:nowrap;cursor:pointer}
.insp .chk input[type=checkbox]{padding:0;width:16px;height:16px;accent-color:var(--ac)}
.fld[hidden],.key[hidden]{display:none}
button:disabled{opacity:.5;cursor:default}
.pl{display:flex;justify-content:space-between;margin:6px 0 8px}
.bar{height:8px;border-radius:4px;background:var(--ln);overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--ac);transition:width .4s}
.bar i.ind{background:linear-gradient(90deg,var(--ln),var(--ac),var(--ln));background-size:200% 100%;animation:mv 1.2s linear infinite}
@keyframes mv{to{background-position:-200% 0}}
@media(prefers-reduced-motion:reduce){.bar i.ind{animation:none;background:var(--ac)}}
.dos button{flex:1}
#ed{position:absolute;inset:0;z-index:5;background:rgba(15,23,32,.97);display:flex;flex-direction:column;padding:12px;text-align:left}
#ed[hidden]{display:none}
.edh{display:flex;align-items:center;gap:10px;margin-bottom:8px}
#edn{color:var(--mu);font-size:13px}
#edl{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:4px}
.er{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:6px}
.er.on{background:rgba(92,200,255,.16)}
.er span{color:var(--mu);font-size:12px;min-width:44px}
.er input{flex:1;min-width:0}
.er button{padding:3px 9px}
#vl{width:84px}
@media(max-height:720px){body{font-size:13px}.cap{display:none}#tl{height:44px}.ic{width:32px;height:32px}}
@media(max-width:900px){
  #ed{position:fixed;z-index:30}
  body{height:auto;min-height:100dvh;overflow:auto}
  main{display:block}
  .work{padding:12px}
  .stage{flex:none;width:100%;aspect-ratio:16/9}
  .insp{border-left:0;border-top:1px solid var(--ln);overflow:visible}
  .insp section.sal{flex:none}
  .pbox{flex:none;height:min(62dvh,480px)}
}
</style>
<header>
  <h1><svg viewBox="0 0 120 120" aria-hidden="true"><rect x="4" y="28.5" width="112" height="63" rx="9" fill="none" stroke="#5cc8ff" stroke-width="3" stroke-opacity=".4"/><rect x="30.75" y="8" width="58.5" height="104" rx="11" fill="#5cc8ff"/><path d="M53 48L53 72L73 60Z" fill="#0f1720" stroke="#0f1720" stroke-width="4" stroke-linejoin="round"/></svg>Clip Vertical</h1>
  <div class="src">
    <input id="url" placeholder="Pega el enlace de un clip o VOD">
    <input id="arch" type="file" accept="video/*">
    <button id="cargar" class="p">Cargar</button><span id="st"></span>
  </div>
</header>
<main>
  <div class="work">
    <div class="head"><h2>Entrada</h2><span class="hint">Arrastra las cajas para elegir qué se ve; cambia el tamaño desde la esquina inferior derecha.</span></div>
    <div class="stage" id="stage"><canvas id="cin" width="640" height="360"></canvas>
      <div id="vacio">Pega un enlace arriba o sube un archivo para empezar.</div>
      <div id="ed" hidden><div class="edh"><b>Editor de subtítulos</b><span id="edn"></span><span class="sp"></span><button id="edx">Cerrar</button></div>
        <p class="hint" style="margin:0 0 8px">Edita el texto; la vista previa de la derecha muestra cómo quedará.</p><div id="edl"></div></div></div>
    <div class="tp">
      <button id="play" class="ic p" aria-label="Reproducir o pausar"></button>
      <span id="t">0 / 0</span>
      <button id="mute" class="ic" aria-label="Silenciar o activar el sonido"></button><input id="vl" type="range" min="0" max="1" step="0.05" value="1" aria-label="Volumen">
      <span id="lab"></span>
      <span class="sp"></span>
      <button id="mi">Marcar inicio</button><button id="mf">Marcar fin</button>
    </div>
    <div id="tl" title="Arrastra los extremos para recortar; haz clic para moverte por el video"><canvas id="film"></canvas><div id="sel"></div><div id="hi" class="hd"></div><div id="hf" class="hd"></div><div id="ph"></div></div>
  </div>
  <aside class="insp">
    <section class="sal">
      <div class="head"><h2>Salida</h2><span class="cap">1080 × 1920</span></div>
      <div class="pbox" id="pbox"><div class="phone"><canvas id="cout" width="270" height="480"></canvas></div></div>
    </section>
    <section>
      <label class="fld"><div class="fh"><span>Diseño</span></div>
        <select id="modo"><option value="dividido">Pantalla dividida</option><option value="centrado">Centrado con fondo difuminado</option></select></label>
      <label class="fld" id="fpct"><div class="fh"><span>Alto del juego</span><b id="pv">60%</b></div>
        <input id="pct" type="range" min="40" max="75" value="60"></label>
      <label class="fld" id="fdesf" hidden><div class="fh"><span>Desenfoque del fondo</span><b id="dv">25</b></div><input id="desf" type="range" min="5" max="40" value="25"></label>
      <div class="key" id="key"><div><span style="background:var(--g)"></span>Juego</div><div><span style="background:var(--o)"></span>Cámara</div></div>
      <div class="row"><label class="chk"><input id="subs" type="checkbox">Subtítulos</label><select id="idioma"><option value="">Detectar idioma</option><option value="es">Español</option><option value="en">English</option></select></div>
      <div class="row dos"><button id="gsub">Generar subtítulos</button><button id="esub" disabled>Editar</button></div>
    </section>
    <section>
      <h2>Presets</h2>
      <div class="row"><select id="pre"></select></div>
      <div class="row"><input id="pn" placeholder="Nombre del preset"><button id="gp">Guardar</button></div>
    </section>
    <button id="comp" class="p big">Compilar clip</button>
    <div id="res"></div>
  </aside>
</main>
<script>
const $=i=>document.getElementById(i),V=document.createElement('video');
V.muted=false;
const cin=$('cin'),cout=$('cout'),ci=cin.getContext('2d'),co=cout.getContext('2d');
const dp=()=>window.devicePixelRatio||1;
let id=null,ini=0,fin=0,drag=null;
const B={juego:{x:.24,y:0,w:.5,h:0,c:'#4ade80'},cam:{x:.7,y:.6,w:.28,h:0,c:'#fb923c'}};
const AJ=()=>Math.round(1920*$('pct').value/100/2)*2,alto=k=>k=='juego'?AJ():1920-AJ();
function fix(k){const b=B[k],a=V.videoWidth/V.videoHeight||16/9,r=a*alto(k)/1080;
  b.w=Math.max(.05,Math.min(b.w,1,1/r));b.h=b.w*r;
  b.x=Math.max(0,Math.min(b.x,1-b.w));b.y=Math.max(0,Math.min(b.y,1-b.h));}
function salida(){const W=cout.width,H=cout.height,s=W/1080;if(!W)return;co.clearRect(0,0,W,H);
  if($('modo').value=='centrado'){const a=V.videoWidth/V.videoHeight,ww=H*a;
    co.filter='blur('+$('desf').value*1.29*s+'px)';co.drawImage(V,(W-ww)/2,0,ww,H);co.filter='none';
    const h=W/a;co.drawImage(V,0,(H-h)/2,W,h);}
  else{let y=0;for(const k of ['cam','juego']){const b=B[k],h=alto(k)*s;
    co.drawImage(V,b.x*V.videoWidth,b.y*V.videoHeight,b.w*V.videoWidth,b.h*V.videoHeight,0,y,W,h);y+=h;}}
  if(window.dibujarSubs)dibujarSubs(W,H,s)}
function draw(){
  if(V.videoWidth){const W=cin.width,H=cin.height;ci.imageSmoothingQuality='high';ci.drawImage(V,0,0,W,H);
    if($('modo').value=='dividido')for(const k in B){fix(k);const b=B[k];
      ci.strokeStyle=ci.fillStyle=b.c;ci.lineWidth=3*dp();ci.strokeRect(b.x*W,b.y*H,b.w*W,b.h*H);
      {const q=8*dp();ci.fillRect((b.x+b.w)*W-q,(b.y+b.h)*H-q,2*q,2*q)}}
    salida();$('t').textContent=V.currentTime.toFixed(2)+' / '+V.duration.toFixed(2);
    }
  requestAnimationFrame(draw);}
draw();
const pos=e=>{const r=cin.getBoundingClientRect();return[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height]};
cin.onmousedown=e=>{if($('modo').value!='dividido')return;const[mx,my]=pos(e);
  for(const k of ['cam','juego']){const b=B[k];
    if(Math.abs(mx-(b.x+b.w))<.025&&Math.abs(my-(b.y+b.h))<.04){drag={k,t:'r'};return}
    if(mx>b.x&&mx<b.x+b.w&&my>b.y&&my<b.y+b.h){drag={k,t:'m',dx:mx-b.x,dy:my-b.y};return}}};
onmousemove=e=>{if(!drag)return;const[mx,my]=pos(e),b=B[drag.k];
  if(drag.t=='r')b.w=mx-b.x;else{b.x=mx-drag.dx;b.y=my-drag.dy}fix(drag.k)};
onmouseup=()=>drag=null;
V.onloadedmetadata=()=>{cin.height=Math.round(640*V.videoHeight/V.videoWidth);
  ini=0;fin=V.duration;lab();V.currentTime=0.1};
V.ontimeupdate=()=>{if(!V.paused&&fin>ini&&V.currentTime>=fin)V.currentTime=ini};
const lab=()=>$('lab').textContent='Inicio '+ini.toFixed(2)+'s · Fin '+fin.toFixed(2)+'s';
$('play').onclick=()=>V.paused?V.play():V.pause();

$('mi').onclick=()=>{ini=V.currentTime;if(fin<=ini)fin=V.duration;lab()};
$('mf').onclick=()=>{fin=V.currentTime;lab()};
$('cargar').onclick=async()=>{const f=new FormData();f.append('url',$('url').value);
  if($('arch').files[0])f.append('archivo',$('arch').files[0]);
  $('st').textContent='Cargando...';
  const d=await(await fetch('/cargar',{method:'POST',body:f})).json();
  if(d.error){$('st').textContent=d.error;return}
  id=d.id;V.src='/video/'+id;$('st').textContent='Listo'};
const P=()=>JSON.parse(localStorage.getItem('p')||'{}');
function lp(){$('pre').innerHTML='<option value="">Elegir preset</option>'+Object.keys(P()).map(n=>'<option>'+n+'</option>').join('')}
lp();
$('gp').onclick=()=>{const n=$('pn').value.trim();if(!n)return;const p=P();
  p[n]={j:[B.juego.x,B.juego.y,B.juego.w],c:[B.cam.x,B.cam.y,B.cam.w],pct:$('pct').value};
  localStorage.setItem('p',JSON.stringify(p));lp();$('pre').value=n};
$('pre').onchange=()=>{const p=P()[$('pre').value];if(!p)return;$('pct').value=p.pct;
  [B.juego.x,B.juego.y,B.juego.w]=p.j;[B.cam.x,B.cam.y,B.cam.w]=p.c};
$('comp').onclick=async()=>{const rs=$('res');
  if(!id){rs.textContent='Carga un video primero.';return}
  $('comp').disabled=true;
  rs.innerHTML='<div class="pl"><span id="pe">Preparando...</span><b id="pp"></b></div><div class="bar"><i id="pbar" class="ind"></i></div>';
  try{
    const body={id,modo:$('modo').value,inicio:ini,fin:fin>=V.duration-.05?0:fin,pct:$('pct').value,desfoque:+$('desf').value,
      subs:$('subs').checked,idioma:$('idioma').value,bloques:window.bloquesParaEnviar?bloquesParaEnviar():null,
      juego:[B.juego.x,B.juego.y,B.juego.w,B.juego.h],camara:[B.cam.x,B.cam.y,B.cam.w,B.cam.h]};
    const r=await(await fetch('/compilar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.error){rs.innerHTML='<pre>'+r.error+'</pre>';return}
    while(true){
      await new Promise(z=>setTimeout(z,500));
      if(!$('pbar')){fetch('/cancelar/'+r.job,{method:'POST'});return}
      const j=await(await fetch('/progreso/'+r.job)).json();
      if(j.estado=='error'){rs.innerHTML='<pre>'+j.error+'</pre>';return}
      if(j.estado=='listo'){rs.innerHTML=(j.aviso?'<p>'+j.aviso+'</p>':'')+
        '<video src="'+j.url+'" controls height="480"></video><br><a href="'+j.url+'" download>Descargar</a>';return}
      $('pe').textContent=j.etapa;$('pp').textContent=j.pct==null?'':Math.round(j.pct)+'%';
      const b=$('pbar');b.style.width=(j.pct==null?100:j.pct)+'%';b.classList.toggle('ind',j.pct==null);
    }
  }catch(e){rs.innerHTML='<pre>'+e+'</pre>'}
  finally{$('comp').disabled=false}};
</script><script>
const PL='<svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 1.5v11l9-5.5z" fill="currentColor"/></svg>',
PA='<svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 2h3v10H3zM8 2h3v10H8z" fill="currentColor"/></svg>';
$('play').innerHTML=PL;
V.addEventListener('play',()=>$('play').innerHTML=PA);
V.addEventListener('pause',()=>$('play').innerHTML=PL);
V.addEventListener('loadedmetadata',()=>$('vacio').hidden=true);
const pv=()=>$('pv').textContent=$('pct').value+'%';
$('pct').addEventListener('input',pv);$('pre').addEventListener('change',pv);
</script>
<script>
const tl=$('tl'),film=$('film'),fc=film.getContext('2d'),F=document.createElement('video');
F.muted=true;F.preload='auto';
let tk=0,dg=null;
const px=t=>t/V.duration*tl.clientWidth;
const tt=e=>{const r=tl.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*V.duration};
async function tiras(){const d=dp(),k=++tk,H=Math.round(56*d),W=Math.round((tl.clientWidth||800)*d);
  film.width=W;film.height=H;fc.fillStyle='#000';fc.fillRect(0,0,W,H);
  F.src=V.src;if(F.readyState<1)await new Promise(r=>F.addEventListener('loadedmetadata',r,{once:true}));
  const n=Math.max(4,Math.ceil(W/(H*F.videoWidth/F.videoHeight))),w=W/n;
  for(let i=0;i<n;i++){if(k!=tk)return;
    F.currentTime=(i+.5)/n*F.duration;
    await new Promise(r=>F.addEventListener('seeked',r,{once:true}));
    fc.drawImage(F,i*w,0,w+1,H);}}
V.addEventListener('loadedmetadata',tiras);
function mover(e){const t=tt(e);
  if(dg=='i'){ini=Math.min(t,fin-.2);V.currentTime=ini;lab()}
  else if(dg=='f'){fin=Math.max(t,ini+.2);V.currentTime=Math.max(ini,fin-.05);lab()}
  else V.currentTime=t}
tl.addEventListener('pointerdown',e=>{if(!V.duration)return;e.preventDefault();
  dg=e.target.id=='hi'?'i':e.target.id=='hf'?'f':'p';mover(e)});
addEventListener('pointermove',e=>{if(dg)mover(e)});
addEventListener('pointerup',()=>dg=null);
function ptl(){if(V.duration){
  $('ph').style.left=px(V.currentTime)-1+'px';
  $('sel').style.left=px(ini)+'px';$('sel').style.width=px(fin-ini)+'px';
  $('hi').style.left=px(ini)+'px';$('hf').style.left=px(fin)-12+'px';}
  requestAnimationFrame(ptl)}
ptl();
addEventListener('keydown',e=>{const a=document.activeElement.tagName;
  if(e.code=='Space'&&!/INPUT|SELECT|TEXTAREA|BUTTON/.test(a)){e.preventDefault();$('play').click()}});
</script>
<script>
const stg=$('stage'),pb=$('pbox');
function fit(){const d=dp();
  let W=stg.clientWidth,H=stg.clientHeight;const a=V.videoWidth?V.videoWidth/V.videoHeight:16/9;
  let w=W,h=W/a;if(h>H){h=H;w=H*a}
  tam(cin,w,h,d);
  W=pb.clientWidth-14;H=pb.clientHeight-14;w=Math.min(W,H*9/16);
  tam(cout,w,w*16/9,d);}
function tam(c,w,h,d){w=Math.max(0,w);h=Math.max(0,h);c.style.width=w+'px';c.style.height=h+'px';
  const pw=Math.round(w*d),ph=Math.round(h*d);if(pw&&(c.width!=pw||c.height!=ph)){c.width=pw;c.height=ph}}
new ResizeObserver(fit).observe(stg);new ResizeObserver(fit).observe(pb);
V.addEventListener('loadedmetadata',fit);addEventListener('resize',fit);fit();
const rs=$('res');
new MutationObserver(()=>{if(rs.innerHTML&&!rs.querySelector('.x'))rs.insertAdjacentHTML('afterbegin','<button class="x">Cerrar</button>')}).observe(rs,{childList:true,characterData:true,subtree:true});
rs.addEventListener('click',e=>{if(e.target.classList.contains('x'))rs.innerHTML=''});
addEventListener('keydown',e=>{if(e.key=='Escape')rs.innerHTML=''});
</script>
<script>
const modo2=()=>{const c=$('modo').value=='centrado';$('fpct').hidden=c;$('key').hidden=c;$('fdesf').hidden=!c};
$('modo').addEventListener('change',modo2);modo2();
$('desf').addEventListener('input',()=>$('dv').textContent=$('desf').value);
</script>
<script>
let SB=[],GEN=false,lastOn=-2;
const ed=$('ed'),edl=$('edl');
const fmt=t=>{const m=Math.floor(t/60);return m+':'+(t-m*60).toFixed(1).padStart(4,'0')};
const efin=()=>fin>=V.duration-.05?V.duration:fin;
function dibujarSubs(W,H,s){
  if(!$('subs').checked||!SB.length)return;
  const t=V.currentTime;let bl=null,ai=-1;
  for(const b of SB){const w=b.w;for(let i=0;i<w.length;i++){
    if(t>=w[i].a&&t<(i+1<w.length?w[i+1].a:w[i].b)){bl=b;ai=i;break}}if(bl)break}
  if(!bl)return;
  co.font='bold '+80*s+'px Arial,sans-serif';co.textBaseline='alphabetic';co.lineJoin='round';co.lineWidth=14*s;
  const sp=co.measureText(' ').width,wd=bl.w.map(o=>co.measureText(o.t).width),y=1384*s;
  let x=(W-(wd.reduce((p,q)=>p+q,0)+sp*(wd.length-1)))/2;
  const xs=wd.map(q=>{const r=x;x+=q+sp;return r});
  co.strokeStyle='#000';bl.w.forEach((o,j)=>co.strokeText(o.t,xs[j],y));
  bl.w.forEach((o,j)=>{co.fillStyle=j==ai?'#ffd23f':'#fff';co.fillText(o.t,xs[j],y)})}
function bloquesParaEnviar(){if(!$('subs').checked||!GEN)return null;
  const f=efin(),r=x=>Math.round(x*1000)/1000,out=[];
  for(const b of SB){const w=b.w.filter(o=>o.a>=ini-.001&&o.a<f).map(o=>[r(Math.max(0,o.a-ini)),r(Math.min(o.b,f)-ini),o.t]);
    if(w.length)out.push(w)}
  return out}
function rehacer(b,v){const p=v.trim().split(/\s+/).filter(Boolean);if(!p.length)return;
  if(p.length==b.w.length){b.w.forEach((o,i)=>o.t=p[i]);return}
  const a=b.w[0].a,c=b.w[b.w.length-1].b,st=(c-a)/p.length;
  b.w=p.map((t,i)=>({a:a+i*st,b:a+(i+1)*st,t}))}
function pintar(){edl.textContent='';lastOn=-2;$('esub').disabled=!SB.length;
  SB.forEach(b=>{const r=document.createElement('div');r.className='er';
    const t=document.createElement('span');t.textContent=fmt(b.w[0].a);
    const inp=document.createElement('input');inp.value=b.w.map(o=>o.t).join(' ');
    inp.onfocus=()=>{V.pause();V.currentTime=b.w[0].a};
    inp.oninput=()=>rehacer(b,inp.value);
    inp.onchange=()=>{if(!inp.value.trim()){SB=SB.filter(q=>q!==b);pintar()}};
    const x=document.createElement('button');x.textContent='×';x.title='Eliminar';
    x.onclick=()=>{SB=SB.filter(q=>q!==b);pintar()};
    r.append(t,inp,x);edl.append(r)});
  $('edn').textContent=SB.length+' bloques'}
function abrirEd(v){ed.hidden=!v;$('esub').textContent=v?'Cerrar editor':'Editar';if(v)pintar()}
$('esub').onclick=()=>abrirEd(ed.hidden);$('edx').onclick=()=>abrirEd(false);
setInterval(()=>{if(ed.hidden)return;const t=V.currentTime;
  const i=SB.findIndex(b=>t>=b.w[0].a&&t<b.w[b.w.length-1].b);
  if(i==lastOn)return;lastOn=i;
  [...edl.children].forEach((r,k)=>r.classList.toggle('on',k==i));
  if(i>=0&&document.activeElement.tagName!='INPUT')edl.children[i].scrollIntoView({block:'nearest'})},120);
async function esperar(job){const rs=$('res');
  while(true){await new Promise(z=>setTimeout(z,500));
    if(!$('pbar')){fetch('/cancelar/'+job,{method:'POST'});return null}
    const j=await(await fetch('/progreso/'+job)).json();
    if(j.estado=='error'){rs.innerHTML='<pre>'+j.error+'</pre>';return null}
    if(j.estado=='listo'){rs.innerHTML='';return j}
    $('pe').textContent=j.etapa;$('pp').textContent=j.pct==null?'':Math.round(j.pct)+'%';
    const b=$('pbar');b.style.width=(j.pct==null?100:j.pct)+'%';b.classList.toggle('ind',j.pct==null)}}
$('gsub').onclick=async()=>{const rs=$('res');
  if(!id){rs.textContent='Carga un video primero.';return}
  $('gsub').disabled=true;
  rs.innerHTML='<div class="pl"><span id="pe">Preparando...</span><b id="pp"></b></div><div class="bar"><i id="pbar" class="ind"></i></div>';
  try{const r=await(await fetch('/subtitulos',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id,inicio:ini,fin:fin>=V.duration-.05?0:fin,idioma:$('idioma').value})})).json();
    if(r.error){rs.innerHTML='<pre>'+r.error+'</pre>';return}
    const j=await esperar(r.job);
    if(j){SB=j.bloques.map(b=>({w:b.map(([a,c,t])=>({a,b:c,t}))}));GEN=true;
      if(SB.length){$('subs').checked=true;abrirEd(true)}
      else{$('esub').disabled=true;rs.innerHTML='<p>No se detectó voz en el tramo elegido.</p>'}}}
  catch(e){rs.innerHTML='<pre>'+e+'</pre>'}
  finally{$('gsub').disabled=false}};
V.addEventListener('loadedmetadata',()=>{SB=[];GEN=false;abrirEd(false);$('esub').disabled=true});
</script>
<script>
let mut=false;
const SON='<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6h3l4-3v10L5 10H2z" fill="currentColor"/><path d="M11.2 5.6a3.4 3.4 0 010 4.8M13 3.8a6 6 0 010 8.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
MUT='<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6h3l4-3v10L5 10H2z" fill="currentColor"/><path d="M11 6l4 4M15 6l-4 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
function vol(){const v=+$('vl').value;V.volume=v;V.muted=mut||v==0;
  $('mute').innerHTML=V.muted?MUT:SON;try{localStorage.setItem('vol',v)}catch(e){}}
try{const sv=localStorage.getItem('vol');if(sv!==null)$('vl').value=sv}catch(e){}
$('vl').addEventListener('input',()=>{mut=false;vol()});
$('mute').addEventListener('click',()=>{mut=!mut;if(!mut&&+$('vl').value==0)$('vl').value=.5;vol()});
vol();
</script>
</html>"""

if __name__ == "__main__":
    limpiar()
    threading.Timer(1, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000)
