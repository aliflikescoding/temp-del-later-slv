import MetaTrader5 as mt5

print("Before init")

ok = mt5.initialize()
print("Init result:", ok)
print("Last error:", mt5.last_error())

print("After init")
