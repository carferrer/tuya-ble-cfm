# Tuya BLE CFM

Integración personalizada de Home Assistant para las cerraduras Tuya BLE de esta instalación. Este fork mantiene un catálogo reducido y el transporte BLE que se ha probado en las cerraduras reales.

Permite controlar las funciones básicas, configurar cuándo conectar por Bluetooth y recuperar registros locales de aperturas y alarmas. Los eventos incluyen la hora original de la cerradura y un identificador de tipo estable para automatizaciones y archivos externos.

## Estado actual

Consolidado en `main`:

- Corrección del acuse de recibo de registros con fecha para recuperar varios registros pendientes en una conexión.
- Eventos de apertura y de alarma independientes, con protección persistente contra duplicados.
- Atributo `event_type_id` para identificar el tipo de apertura o alarma.
- Opciones de conexión por cerradura y sincronización periódica.
- Corrección de los dominios sugeridos de `entity_id`, conservando los `unique_id` y las entidades registradas.
- Validación de frames BLE, recuperación ante fragmentos inválidos y contención de errores de longitud.

Se han probado físicamente la recuperación de varias aperturas, los accesos por huella y código, las alarmas de huella/código incorrectos y los avisos por Pushover con `event_type_id`. Los demás tipos están implementados y cubiertos por pruebas automáticas, pero todavía requieren validación física.

## Cerraduras compatibles

| Product ID | Modelo / categoría | Funciones básicas | Eventos Access / Alarm events y recuperación DP69 |
| --- | --- | --- | --- |
| `b3aouluh` | Smart Lock / `jtmspro` | Sí | Sí |
| `okkyfgfs` | P196_V / `ms` | Sí | Pendientes de validar y adaptar |

La instalación de referencia utiliza cuatro `b3aouluh` y una `okkyfgfs`. La compatibilidad de otros productos Tuya no está garantizada.

## Instalación y actualización

### HACS

1. Añade `https://github.com/carferrer/tuya-ble-cfm` como repositorio personalizado de tipo **Integración**.
2. Instala Tuya BLE CFM.
3. Reinicia Home Assistant.
4. Añade o configura la integración Tuya BLE en **Ajustes → Dispositivos y servicios**.

Un cambio en `main` no implica que se haya publicado una nueva release para HACS. Comprueba qué versión estás instalando.

### Instalación manual de main

1. Descarga [main en ZIP](https://github.com/carferrer/tuya-ble-cfm/archive/refs/heads/main.zip).
2. Copia la carpeta `custom_components/tuya_ble` del ZIP a `/config/custom_components/tuya_ble`.
3. Asegúrate de copiar todos los archivos, incluidos `alarm.py` y `alarm_event.py`.
4. Reinicia Home Assistant y recarga la página del navegador.

Las nuevas entidades se pueden localizar en **Herramientas para desarrolladores → Estados** y en la ficha del dispositivo. Los nombres concretos dependen del nombre asignado a la cerradura y de posibles personalizaciones.

Home Assistant necesita acceso BLE a la cerradura, mediante un adaptador compatible o un proxy Bluetooth ESPHome. La recuperación de registros descrita aquí es local por BLE; no requiere un gateway Tuya Wi-Fi/Bluetooth para descargar esos registros.

## Entidades

| Entidad | DP / función |
| --- | --- |
| Botón `bluetooth_unlock` | DP6, desbloqueo Bluetooth |
| Botón `Actualizar cerradura` | Conexión BLE manual y solicitud de estado actual, ambas familias |
| Selector `beep_volume` | DP31, volumen |
| Sensor binario `lock_motor_state` | DP47, estado del motor |
| Sensor `Alarm` | DP21, último valor de alarma recibido |
| Batería | DP8 en `okkyfgfs`; DP9 `battery_state` en `b3aouluh` |
| RSSI | Diagnóstico de señal |
| Evento `Access` | Aperturas, solo `b3aouluh` |
| Sensor `Last access` | Hora de la apertura más reciente, solo `b3aouluh` |
| Evento `Alarm events` | Registros individuales DP21, solo `b3aouluh` |
| Interruptor `Modo paso libre` | DP33, solo `b3aouluh`; entidad habilitada por defecto |
| Sensor `Last connected` | Última conexión BLE emparejada, ambas familias |
| Sensor `Update interval` | Intervalo periódico en minutos; 0 en los otros modos, ambas familias |

El sensor `Alarm` muestra un estado. `Alarm events` permite reaccionar a cada registro nuevo, incluidos varios fallos consecutivos del mismo tipo con horas distintas. Las alarmas no se convierten en aperturas ni modifican `Last access`.

El sensor `alarm_lock` guarda por cerradura su último valor DP21 válido y lo recupera tras reiniciar HA. Al actualizar desde una versión anterior, también puede tomar el último registro de alarma que la integración ya había archivado localmente. Una nueva lectura DP21 tiene prioridad sobre el valor guardado. La recuperación no abre una conexión BLE ni vuelve a emitir eventos; hasta recibir una lectura nueva, el estado mostrado es el último conocido. Si una cerradura nunca ha comunicado una alarma y no hay registro previo, el sensor seguirá sin valor hasta el primer DP21.

El interruptor Modo paso libre refleja el DP33 confirmado por la cerradura. Habilitar la entidad no activa físicamente el modo. Las entidades deshabilitadas por defecto en la versión experimental se habilitan al cargar la integración; una deshabilitación manual del usuario se respeta. Se conserva el mismo `unique_id` y `entity_id`. El evento `Access` emite `passage_mode_enabled` (`event_type_id: 33`) solo cuando DP33 pasa de desactivado a activado. No se emite al cerrarlo, con informes repetidos ni al arrancar HA con el modo ya activado. La hora del evento es la de recepción en HA; no es un registro histórico de apertura.

`Last connected` conserva durante la desconexión la hora de la última conexión BLE emparejada vista por este proceso de HA. Tras un reinicio empieza sin valor hasta la siguiente conexión. `Update interval` muestra los minutos configurados en modo periódico; muestra 0 en los modos bajo demanda, detección de actividad y keep-alive. Ninguno de estos sensores abre conexiones BLE adicionales. El botón `Actualizar cerradura` fuerza una conexión y una lectura de estado únicamente cuando se pulsa; la conexión vuelve a seguir el modo de ahorro configurado.

## Conexión y ahorro de batería

La política se configura por cerradura en las opciones de la integración.

| Modo | Funcionamiento |
| --- | --- |
| Solo bajo demanda (`on_demand`) | Sin sincronización periódica ni reconexión por anuncios de actividad; conecta cuando una operación lo requiere. |
| Detectar actividad (`power_save`) | Utiliza el detector de actividad de anuncios BLE para intentar conectar. Es el modo predeterminado; no garantiza detectar cada acción física. |
| Sincronización periódica (`periodic_sync`) | Conecta cada intervalo configurado para actualizar y recuperar registros. La detección por anuncios queda desactivada. |
| Siempre conectada (`keep_alive`) | Desactiva la desconexión por inactividad y utiliza un supervisor de reconexión cada 30 segundos. |

Intervalos disponibles: **1, 2, 5, 10, 15, 30 y 60 minutos; 5, 12 y 24 horas**. El valor predeterminado del intervalo es 5 minutos y se utiliza en modo periódico.

El ahorro utiliza desconexión por inactividad, con un plazo base de 30 segundos y ajustes según la operación y el modo. Las entidades de eventos aprovechan las conexiones existentes; no añaden sondeos propios.

En modo periódico, los avisos se reciben al sincronizar, no necesariamente en el momento del suceso. Un intervalo mayor reduce la frecuencia de conexiones y aumenta la espera de los avisos. No se ha medido todavía la autonomía final de las baterías.

## Recuperación de registros locales

En `b3aouluh`, DP69 participa en la recuperación de registros pendientes. La integración responde a la solicitud de la cerradura y confirma los registros con fecha mediante un acuse de recibo de un byte de éxito (`0x00`).

La corrección del acuse de recibo permitió comprobar que se reciben varios registros en una misma conexión, tanto aperturas como alarmas. No se asume una capacidad concreta de almacenamiento de la cerradura ni una conservación ilimitada.

Los registros pueden llegar del más reciente al más antiguo. Por eso:

- `event_time` conserva la hora original del suceso.
- `received_at` indica cuándo lo recibió la integración.
- `Last access` conserva la mayor fecha de apertura, aunque llegue después un registro más antiguo.
- El estado de una entidad `event.*` es la hora de emisión en Home Assistant; para archivar el suceso se debe usar `event_time`.

### Protección contra duplicados

Access y Alarm events guardan por separado hasta **200 claves por cerradura**, persistidas en el almacenamiento de Home Assistant.

- Un registro ya conocido dentro de esa ventana no vuelve a emitir un evento.
- En la primera inicialización, el historial que ya esté en memoria establece una base silenciosa.
- Los registros nuevos que lleguen después sí pueden emitir avisos, incluso si son antiguos y se acaban de recuperar de la cerradura.
- En posteriores cargas, se procesan los registros del historial que aún no se hayan visto.
- Un registro expulsado de la ventana de 200 claves podría volver a emitirse si se recibe de nuevo.
- Dos registros con el mismo DP, hora y valor no se pueden distinguir.

Añadir un atributo nuevo, como `event_type_id`, no fuerza a reproducir eventos antiguos: aparecerá con el siguiente registro nuevo.

## Catálogo de aperturas

| DP | event_type_id | event_type | method |
| --- | --- | --- | --- |
| 12 | 12 | `fingerprint_unlock` | `fingerprint` |
| 13 | 13 | `password_unlock` | `password` |
| 14 | 14 | `dynamic_password_unlock` | `dynamic_password` |
| 15 | 15 | `card_unlock` | `card` |
| 19 | 19 | `ble_unlock` | `ble` |
| 55 | 55 | `temporary_password_unlock` | `temporary_password` |
| 62 | 62 | `phone_remote_unlock` | `phone_remote` |
| 63 | 63 | `voice_remote_unlock` | `voice_remote` |

Se aceptan los DP de apertura declarados como `DT_VALUE`. La integración emite únicamente los registros que recibe; no deduce una apertura a partir del estado del motor o de haber pulsado el botón de desbloqueo.

## Catálogo de alarmas

Todas utilizan **DP21**, de tipo `DT_ENUM`.

| alarm_value | event_type_id | event_type |
| --- | --- | --- |
| 0 | 2100 | `wrong_finger` |
| 1 | 2101 | `wrong_password` |
| 2 | 2102 | `wrong_card` |
| 3 | 2103 | `wrong_face` |
| 4 | 2104 | `tongue_bad` |
| 5 | 2105 | `too_hot` |
| 6 | 2106 | `unclosed_time` |
| 7 | 2107 | `tongue_not_out` |
| 8 | 2108 | `pry` |
| 9 | 2109 | `key_in` |
| 10 | 2110 | `low_battery` |
| 11 | 2111 | `power_off` |
| 12 | 2112 | `shock` |

Los valores de alarma desconocidos o los registros con fechas inválidas se ignoran en la entidad de eventos.

## Atributos de los eventos

| Atributo | Contenido |
| --- | --- |
| `event_type` | Nombre del tipo, según los catálogos |
| `event_type_id` | Entero estable: DP de apertura o 2100 + enum de alarma |
| `dp_id` | DP de origen |
| `event_time` | Fecha original, ISO 8601 en UTC |
| `received_at` | Fecha de recepción, ISO 8601 en UTC |
| `delay_seconds` | Diferencia entre recepción y suceso, limitada a un mínimo de cero |
| `recovered` | Verdadero cuando la diferencia supera 2 segundos; es una clasificación por retraso |
| `method` | Método de apertura, solo Access |
| `access_value` | Entero original del registro de apertura |
| `member_id` | Alias de `access_value` por compatibilidad |
| `alarm_value` | Enum original de alarma, solo Alarm events |

`access_value` no se interpreta como una identidad de usuario confirmada: su significado depende del método y del dispositivo. `event_type_id` es una convención de esta integración, no un identificador de registro proporcionado por Tuya.

## Ejemplo: aperturas y alarmas por Pushover

Sustituye las entidades y la acción `notify.pushover` por las de tu instalación. El ejemplo utiliza la cerradura del salón.

La automatización escucha cambios de las entidades de evento. Utiliza `trigger.to_state.attributes` para conservar los datos de cada aviso cuando se recibe un lote. El modo en cola admite hasta 100 ejecuciones pendientes/activas.

```yaml
alias: "Accesos y alarmas salón → Pushover"
triggers:
  - trigger: state
    entity_id:
      - event.cerradura_puerta_salon_access
      - event.cerradura_puerta_salon_alarm_events
    not_to:
      - "unavailable"
      - "unknown"

conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state is not none
         and trigger.to_state.attributes.get('event_time') is not none
         and trigger.to_state.attributes.get('received_at') is not none }}

actions:
  - action: notify.pushover
    data:
      title: >-
        {% if trigger.to_state.attributes.get('dp_id') == 21 %}
          Alarma puerta salón
        {% else %}
          Apertura puerta salón
        {% endif %}
      message: |-
        {% set a = trigger.to_state.attributes %}
        Tipo: {{ a.get('event_type', 'desconocido') }}
        ID de tipo: {{ a.get('event_type_id', 'sin ID') }}
        {% if a.get('dp_id') != 21 %}
        Método: {{ a.get('method', 'desconocido') }}
        {% endif %}
        Hora suceso: {{ as_local(as_datetime(a['event_time'])).strftime('%d/%m/%Y %H:%M:%S') }}
        Hora recepción: {{ as_local(as_datetime(a['received_at'])).strftime('%d/%m/%Y %H:%M:%S') }}
        DP: {{ a.get('dp_id') }}
        Valor Tuya: {{ a.get('alarm_value', a.get('access_value')) }}
        Recuperado: {{ 'Sí' if a.get('recovered') else 'No' }}
        Retraso: {{ a.get('delay_seconds') }} segundos

mode: queued
max: 100
```

Para probarla, genera un nuevo acceso o alarma y espera la sincronización. Ejecutar manualmente las acciones no proporciona el contexto `trigger` que utiliza la plantilla. Los registros históricos nuevos para la integración también pueden generar notificaciones.

Referencias: [entidades de evento](https://www.home-assistant.io/integrations/event/), [Pushover](https://www.home-assistant.io/integrations/pushover/).

## Archivo externo en MSSQL

La integración proporciona los datos; **no escribe directamente en MSSQL**. La automatización o servicio que los archive debe guardar cada evento recibido.

Modelo de campos propuesto:

| Campo | Uso |
| --- | --- |
| `Id` | `BIGINT IDENTITY`, clave de cada fila |
| `DeviceId` | Identificador estable de la cerradura, asignado o resuelto por el archivador |
| `EventTypeId` | `event_type_id`, referencia al catálogo de tipos |
| `EventTimeUtc` | Hora original normalizada a UTC |
| `ReceivedAtUtc` | Hora de recepción normalizada a UTC |
| `RawValue` | `access_value` o `alarm_value` |

El tipo de evento y la fila del histórico tienen identificadores distintos. Para evitar reinserciones puede usarse una clave única sobre `DeviceId + EventTypeId + EventTimeUtc + RawValue`, conservando la precisión de la fecha original. Dos sucesos idénticos con el mismo timestamp de la cerradura no se podrán distinguir. `DeviceId` no es un atributo añadido a estos eventos: debe aportarlo el archivador a partir de la entidad de origen.

## Compatibilidad y robustez BLE

Los constructores sugieren IDs en su dominio correcto (`button`, `binary_sensor`, `select` o `sensor`). Los `unique_id` existentes se conservan; no se renombran ni eliminan entidades registradas para aplicar esta corrección.

El parser valida longitudes, estructura, cifrado y CRC. Los frames válidos fragmentados se recomponen; los inválidos o incompletos se descartan y el receptor puede recuperarse con el siguiente inicio de frame. Los errores de longitud de datos se contienen en el procesamiento de notificaciones.

Los problemas recuperables de orden de paquetes se registran en debug y los frames inválidos en warning. Estas mejoras no cambian el protocolo DP69 ni convierten una conexión fallida en una conexión válida.

Se conserva el transporte BLE de este fork. La migración al transporte upstream 0.12.x no se incorporó porque las pruebas físicas mostraron regresiones.

## Diagnóstico y trabajo pendiente

El diagnóstico de la integración incluye DP recibidos con sus horas, actividad BLE, contadores de recuperación DP69 y estado de la política de conexión.

Se han observado fallos `starting notifications failed`, `GATT Error 133`, desconexiones inesperadas y algún timeout. Una señal débil puede influir, pero el mensaje por sí solo no identifica la causa. La mejora de limpieza de conexiones fallidas, reintentos y nivel de log está **pendiente**; no forma parte de los cambios de eventos ya validados.

También quedan pendientes:

- Obtener y validar los DP de eventos de la otra cerradura, `okkyfgfs`.
- Probar físicamente los métodos de apertura y alarmas aún no ensayados.
- Medir la autonomía real con los distintos intervalos de conexión.
- Implementar, si se necesita, el envío externo a MSSQL.

## Prueba experimental: botón interior de paso libre

Los diagnósticos de una cerradura `b3aouluh` mostraron DP33 (`DT_BOOL`) y DP47 en `false` en estado normal, `true` con el botón interior en modo abierto y de nuevo `false` tras desactivarlo. La prueba física posterior confirmó que encender y apagar DP33 desde HA cambia el modo de paso libre en esa cerradura. Las otras cerraduras `b3aouluh` aún requieren validación física.

La rama experimental añade el interruptor **Modo paso libre** solo para `b3aouluh`, habilitado por defecto como entidad. No cambia DP6, DP47, DP69 ni la política de conexiones. Para comprobarlo en otra cerradura:

1. Instala la rama experimental y reinicia Home Assistant.
2. En **Ajustes → Dispositivos y servicios → Entidades**, busca el interruptor en la cerradura elegida. Debe mostrar el estado reportado de DP33: apagado en normal, encendido al activar el botón interior. Si el estado no coincide, detén la prueba. Una entidad deshabilitada manualmente por el usuario seguirá deshabilitada hasta volver a habilitarla.
3. Con la cerradura a la vista y pudiendo volver a normal físicamente, enciende el interruptor en HA. Comprueba físicamente si queda en paso libre y si HA recibe DP33 `true`.
4. Apágalo desde HA. Comprueba que vuelve a normal y que HA recibe DP33 `false`. Si no hay confirmación en 15 segundos, la acción mostrará un error; el efecto físico puede haber ocurrido igualmente, así que inspecciona la cerradura antes de repetirla.
5. Guarda diagnósticos y los mensajes del registro para comparar DP33 y DP47.

El interruptor solo acepta un DP33 Booleano previamente reportado. La escritura espera una notificación posterior de la cerradura con el valor solicitado. El transporte modifica su caché local al enviar un DP, por lo que ese valor local **no** se considera confirmación. La entidad muestra la última lectura recibida y puede estar desactualizada mientras la cerradura esté desconectada. No se programa ningún cambio automático ni se fuerza una reversión al agotar el tiempo de espera.

## Validación

La versión consolidada de eventos e IDs pasó **88 pruebas**, Ruff, Hassfest y HACS. Las pruebas cubren el parser BLE, el alcance de los productos, las políticas de conexión, los IDs de entidades y el comportamiento de los eventos: tipos, fechas, duplicados, carga inicial y recargas.

Las pruebas de entidades utilizan dobles ligeros de los límites de Home Assistant. Se complementan con las pruebas físicas descritas arriba; no equivalen a haber probado todos los tipos de evento en todas las cerraduras.

## Créditos y licencia

Derivado del proyecto `ha-tuya-ble/ha_tuya_ble` y de sus colaboradores.

Licencia MIT. Consulta los archivos de licencia incluidos en el repositorio.
