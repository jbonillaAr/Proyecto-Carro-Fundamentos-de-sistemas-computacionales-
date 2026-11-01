import machine
import sys
import select
import time
import utime
from machine import ADC, Pin

# registro de corrimiento 74LS164, a y b van juntos a gp15
AB = machine.Pin(15, machine.Pin.OUT)
CLK = machine.Pin(14, machine.Pin.OUT)


def ejecutarSecuencia(secuencia):
    # envía los 8 bits al registro, el primero de la lista queda en QA
    for i in range(8):
        bit = secuencia[7 - i]
        AB(bit)
        # un pulso de reloj corre los bits una posición
        CLK(1)
        CLK(0)


# divisor de voltaje entre la ldr y la resistencia de 10 kΩ, conectado a gp27
adcLdr = ADC(Pin(27))

# convierte la lectura de 0 a 65535 en voltios de 0 a 3.3
convVolt = 3.3 / 65535


def leerFotoresistencia():
    # solo para calibrar los umbrales, main no la llama
    # para usarla, cambiar main() por leerFotoresistencia() al final del archivo
    print("iniciando lectura de la fotoresistencia en gp27")

    try:
        while True:
            valorAdc = adcLdr.read_u16()
            voltaje = valorAdc * convVolt
            print(f"Valor ADC: {valorAdc:5d} | Voltaje: {voltaje:.2f} V")
            utime.sleep(0.5)
    except KeyboardInterrupt:
        print("\nPrueba Finalizada")


# pines del L298N, con números gp y no físicos
# enA e in1, in2 mueven la dirección. enB e in3, in4 mueven la tracción
enA = machine.PWM(machine.Pin(16))
in1 = machine.Pin(17, machine.Pin.OUT)
in2 = machine.Pin(18, machine.Pin.OUT)
enB = machine.PWM(machine.Pin(19))
in3 = machine.Pin(20, machine.Pin.OUT)
in4 = machine.Pin(21, machine.Pin.OUT)

enA.freq(1000)
enB.freq(1000)

# potencia mínima con la que arranca el motor de tracción, calibrar
potenciaMinima = 50000
potenciaMaxima = 65535

# el motor de dirección siempre va a fondo
potenciaDireccion = 65535

# tiempos en segundos, calibrar con el carro en el piso
tiempoGiro = 1.5
tiempoCirculo = 6.0
tiempoZigzag = 0.8
pasosZigzag = 6

# nivel que usan dir y los patrones cuando el carro está detenido
nivelDefecto = 3

# posición de cada luz en la secuencia (qa es la posición 0, qf la 5)
bitsFrontales = (0, 1)    # qa y qb
bitsTraseras = (2, 3)     # qc y qd
bitIzquierda = 4          # qe
bitDerecha = 5            # qf

# histéresis de la ldr, calibrar con leerFotoresistencia()
# por debajo de umbralOscuro se enciende, por encima de umbralClaro se apaga
# entre los dos valores no se cambia nada, así no titila cerca del límite
umbralOscuro = 20000
umbralClaro = 25000

# cada cuánto se revisa la ldr en modo automático, en milisegundos
periodoLdr = 500

# tabla que se imprime al inicio y con el comando ayuda
tablaComandos = """
comando     valores              función
pwm         -4 a 4               potencia, negativo retrocede, 0 detiene
dir         -1, 0, 1, 2          izquierda, recto, derecha, retroceder
lf          0, 1                 luces frontales
lb          0, 1                 luces traseras
ll          0, 1                 direccional izquierda
lr          0, 1                 direccional derecha
ldr         0, 1                 luces frontales y traseras automáticas con la ldr
Circle      sin valor            da una vuelta y se detiene
Infinite    sin valor            recorre un 8 y se detiene
ZigZag      sin valor            avanza en zigzag
especial    sin valor            maniobra de parqueo en reversa
ayuda       sin valor            muestra esta tabla
salir       sin valor            apaga todo y termina

ejemplos: pwm 3, dir -1, lf 1, ldr 1, circle
"""


def nivelAPotencia(nivel):
    # convierte un nivel de 1 a 4 en una potencia entre potenciaMinima y potenciaMaxima
    if nivel == 0:
        return 0
    return potenciaMinima + (potenciaMaxima - potenciaMinima) * (abs(nivel) - 1) // 3


def ponerTraccion(nivel):
    # positivo avanza, negativo retrocede, cero suelta el motor
    # el sentido anterior se lee de los pines: 1 adelante, -1 atrás, 0 detenido
    sentidoAnterior = in3.value() - in4.value()

    # pausa corta si cambia de sentido para no forzar el driver
    if nivel * sentidoAnterior < 0:
        enB.duty_u16(0)
        time.sleep(0.1)

    # in3 en 1 e in4 en 0 avanza, al revés retrocede, los dos en 0 se suelta
    if nivel > 0:
        in3.value(1)
        in4.value(0)
    elif nivel < 0:
        in3.value(0)
        in4.value(1)
    else:
        in3.value(0)
        in4.value(0)

    # la potencia va por enB con pwm
    enB.duty_u16(nivelAPotencia(nivel))


def ponerDireccion(direccion):
    # -1 izquierda, 0 recto, 1 derecha
    # recto apaga el motor de dirección
    if direccion == 0:
        in1.value(0)
        in2.value(0)
        enA.duty_u16(0)
        return

    # igual que en la tracción, in1 e in2 opuestos escogen el lado
    in1.value(1 if direccion < 0 else 0)
    in2.value(0 if direccion < 0 else 1)
    enA.duty_u16(potenciaDireccion)


def detenerTodo():
    # ruedas rectas y tracción apagada
    ponerDireccion(0)
    ponerTraccion(0)


def ponerLuces(estado, bits, valor):
    # cambia las luces indicadas y envía la secuencia completa al registro
    # el registro no se puede leer, por eso el estado se guarda en estado["luces"]
    for bit in bits:
        estado["luces"][bit] = valor
    ejecutarSecuencia(estado["luces"])


def revisarLdr(estado):
    # enciende las 4 luces si está oscuro y las apaga si hay luz
    # usa histéresis: en la zona entre los dos umbrales no cambia nada
    lectura = adcLdr.read_u16()

    # poca luz da lectura baja, entonces está oscuro
    if lectura < umbralOscuro:
        oscuro = 1
    elif lectura > umbralClaro:
        oscuro = 0
    else:
        return

    # solo envía al registro si cambió, para no reenviar lo mismo cada vez
    if estado["luces"][bitsFrontales[0]] != oscuro:
        ponerLuces(estado, bitsFrontales + bitsTraseras, oscuro)


def esperar(estado, segundos):
    # reemplaza a time.sleep en los movimientos
    # así el modo ldr sigue funcionando mientras el carro se mueve
    fin = time.ticks_add(time.ticks_ms(), int(segundos * 1000))
    ultimaRevision = time.ticks_ms()

    # mientras no llegue al tiempo, revisa la ldr cada periodoLdr
    while time.ticks_diff(fin, time.ticks_ms()) > 0:
        if estado["ldr"] and time.ticks_diff(time.ticks_ms(), ultimaRevision) >= periodoLdr:
            revisarLdr(estado)
            ultimaRevision = time.ticks_ms()
        time.sleep(0.02)


def manejar(estado, nivel, direccion, segundos):
    # mueve el carro con un nivel y una dirección durante un tiempo
    ponerDireccion(direccion)
    ponerTraccion(nivel)
    esperar(estado, segundos)


# comandos de mando, cada uno recibe el estado del carro y lo actualiza

def comandoPwm(estado, valor):
    # cambia la potencia y la guarda para que dir la use
    ponerTraccion(valor)
    estado["nivel"] = valor
    print("potencia en", valor)


def comandoDir(estado, valor):
    # si el carro está detenido se mueve con el nivel de defecto
    nivel = estado["nivel"] if estado["nivel"] != 0 else nivelDefecto

    # 2 retrocede recto, 0 avanza recto, -1 y 1 giran
    if valor == 2:
        print("retrocediendo")
        manejar(estado, -abs(nivel), 0, tiempoGiro)
    elif valor == 0:
        print("recto")
        manejar(estado, nivel, 0, tiempoGiro)
    else:
        # enciende la direccional del lado del giro mientras dura
        bit = bitIzquierda if valor == -1 else bitDerecha
        direccionalAntes = estado["luces"][bit]
        print("girando", "izquierda" if valor == -1 else "derecha")
        ponerLuces(estado, (bit,), 1)
        manejar(estado, nivel, valor, tiempoGiro)
        ponerLuces(estado, (bit,), direccionalAntes)

    # vuelve al nivel que tenía antes, con las ruedas rectas
    ponerDireccion(0)
    ponerTraccion(estado["nivel"])


def comandoLf(estado, valor):
    # enciende o apaga las frontales, las otras tres funciones son igual
    ponerLuces(estado, bitsFrontales, valor)
    print("luces frontales", "encendidas" if valor else "apagadas")


def comandoLb(estado, valor):
    ponerLuces(estado, bitsTraseras, valor)
    print("luces traseras", "encendidas" if valor else "apagadas")


def comandoLl(estado, valor):
    ponerLuces(estado, (bitIzquierda,), valor)
    print("direccional izquierda", "encendida" if valor else "apagada")


def comandoLr(estado, valor):
    ponerLuces(estado, (bitDerecha,), valor)
    print("direccional derecha", "encendida" if valor else "apagada")


def comandoLdr(estado, valor):
    # activa o desactiva el modo automático, la revisión la hacen esperar y leerLinea
    estado["ldr"] = valor == 1

    if estado["ldr"]:
        print("control automático de luces activado")
        revisarLdr(estado)
    else:
        # al desactivar, las luces quedan como estaban
        print("control automático de luces desactivado")


# patrones, todos terminan con el carro detenido y nivel en 0

def patronCirculo(estado):
    # una vuelta completa girando a la derecha
    print("círculo")
    manejar(estado, nivelDefecto, 1, tiempoCirculo)
    detenerTodo()
    estado["nivel"] = 0


def patronInfinito(estado):
    # un círculo a la izquierda y otro a la derecha forman el 8
    print("infinito")
    manejar(estado, nivelDefecto, -1, tiempoCirculo)
    manejar(estado, nivelDefecto, 1, tiempoCirculo)
    detenerTodo()
    estado["nivel"] = 0


def patronZigzag(estado):
    # alterna izquierda y derecha, empieza por la izquierda
    print("zigzag")
    for i in range(pasosZigzag):
        # pasos pares a la izquierda, impares a la derecha
        direccion = -1 if i % 2 == 0 else 1
        manejar(estado, nivelDefecto, direccion, tiempoZigzag)
    detenerTodo()
    estado["nivel"] = 0


def patronEspecial(estado):
    # parqueo en reversa, como meterse en un espacio a la derecha
    print("movimiento especial")
    # avanza, retrocede girando a un lado y luego al otro, y se acomoda
    manejar(estado, nivelDefecto, 0, 1.0)
    manejar(estado, -nivelDefecto, 1, 1.2)
    manejar(estado, -nivelDefecto, -1, 1.2)
    manejar(estado, nivelDefecto, 0, 0.4)
    detenerTodo()
    estado["nivel"] = 0


# comandos que llevan un valor, con su función y valores válidos
comandosConValor = {
    "pwm": (comandoPwm, (-4, -3, -2, -1, 0, 1, 2, 3, 4)),
    "dir": (comandoDir, (-1, 0, 1, 2)),
    "lf": (comandoLf, (0, 1)),
    "lb": (comandoLb, (0, 1)),
    "ll": (comandoLl, (0, 1)),
    "lr": (comandoLr, (0, 1)),
    "ldr": (comandoLdr, (0, 1)),
}

# comandos sin valor, se escriben en minúscula porque la entrada se pasa a minúscula
patrones = {
    "circle": patronCirculo,
    "infinite": patronInfinito,
    "zigzag": patronZigzag,
    "especial": patronEspecial,
}


def ejecutarComando(estado, linea):
    # separa el comando y el valor, revisa que sean válidos y llama la función
    partes = linea.strip().lower().split()
    if not partes:
        return

    nombre = partes[0]

    # cada revisión que falla avisa y sale sin mover nada
    if nombre == "ayuda":
        print(tablaComandos)
        return

    if nombre in patrones:
        if len(partes) != 1:
            print(nombre, "no lleva valor")
            return
        patrones[nombre](estado)
        print("patrón terminado")
        return

    if nombre not in comandosConValor:
        print("comando desconocido, escriba ayuda para ver la tabla")
        return

    # saca la función y los valores permitidos del diccionario
    funcion, valoresValidos = comandosConValor[nombre]

    if len(partes) != 2:
        print(nombre, "necesita un valor:", valoresValidos)
        return

    try:
        valor = int(partes[1])
    except ValueError:
        print("el valor tiene que ser un número entero")
        return

    if valor not in valoresValidos:
        print("valor fuera de rango, use uno de", valoresValidos)
        return

    funcion(estado, valor)


def leerLinea(estado):
    # espera una línea de la terminal sin bloquear el programa
    # así puede revisar la ldr mientras el usuario no ha escrito nada
    lector = select.poll()
    lector.register(sys.stdin, select.POLLIN)
    linea = ""
    ultimaRevision = time.ticks_ms()

    while True:
        # lee un caracter solo si hay algo escrito
        if lector.poll(0):
            caracter = sys.stdin.read(1)
            # el enter termina la línea, lo demás se va pegando
            if caracter in ("\n", "\r"):
                if linea:
                    return linea
            else:
                linea += caracter

        if estado["ldr"] and time.ticks_diff(time.ticks_ms(), ultimaRevision) >= periodoLdr:
            revisarLdr(estado)
            ultimaRevision = time.ticks_ms()

        time.sleep(0.01)


def main():
    # nivel de potencia, las 8 posiciones del registro y si el modo ldr está activo
    estado = {"nivel": 0, "luces": [0, 0, 0, 0, 0, 0, 0, 0], "ldr": False}

    # arranca con motores y luces apagados
    detenerTodo()
    ejecutarSecuencia(estado["luces"])
    print(tablaComandos)

    try:
        while True:
            print("> ", end="")
            linea = leerLinea(estado)
            if linea.strip().lower() == "salir":
                break
            ejecutarComando(estado, linea)
    except KeyboardInterrupt:
        print()
    finally:
        # pase lo que pase, deja todo apagado
        detenerTodo()
        ejecutarSecuencia([0, 0, 0, 0, 0, 0, 0, 0])
        print("motores y luces apagados")


main()
