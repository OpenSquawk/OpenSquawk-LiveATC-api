- als erstes takeoff
- zustandsautomat ganz basic, nur was wir brauchen


ABlauf:
ATC: callsign, wind xx, y knots, runway zz, cleared for takeoff
Readback: cleared for takeoff runway zz, callsign
nach abheben:
ATC: callsign, contact departure at freq xxx...
Readback: xxx, callsign


class StateManager
wenn state next state urück gibt, dann wird geguckt ob der alle enryconditions true hat und dann wird gewechselt

State
entryConditions
run (atc transmission geben oder pilot transmission bekommen
carryData
getnextState method, gibt none zurück wenn noch nicht weiter gegangen wird und sonst nextState.
einmal nextState wenn pilonUtterance kommt
einmal nextState on Telemetry update
