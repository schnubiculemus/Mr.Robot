import sys

def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    if b == 0:
        return "Fehler: Division durch Null nicht erlaubt"
    return a / b

operations = {
    '+': add,
    '-': subtract,
    '*': multiply,
    '/': divide
}

def calculator():
    print("=== Einfacher Taschenrechner ===")
    print("Verfügbare Operatoren: + - * /")
    print("Zum Beenden 'q' eingeben\n")
    
    while True:
        operator = input("Operator eingeben: ")
        if operator == 'q':
            print("Taschenrechner beendet.")
            break
        
        if operator not in operations:
            print("Ungueltiger Operator. Bitte +, -, * oder / eingeben.\n")
            continue
        
        try:
            num1 = float(input("Erste Zahl: "))
            num2 = float(input("Zweite Zahl: "))
        except ValueError:
            print("Ungueltige Eingabe. Bitte Zahlen eingeben.\n")
            continue
        
        result = operations[operator](num1, num2)
        print(f"Ergebnis: {num1} {operator} {num2} = {result}\n")

if __name__ == "__main__":
    calculator()