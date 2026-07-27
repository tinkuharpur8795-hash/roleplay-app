import json
import os
from dataclasses import dataclass, asdict

@dataclass
class SceneState:
    # 1. Bake the temporal anchor directly into the default environment
    environment: str = "[Time: Start of Scene] Unknown location."
    
    # 2. Explicitly note that nothing has happened yet
    physical_state: str = "Fully clothed, standing normally. No recent actions completed."
    
    active_intentions: str = "None."
    
    # NEW: Store the 1-sentence action bridge from the Brain
    recent_action_bridge: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_save_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SceneState':
        """Instantiates state, migrating old slot-based saves safely."""
        instance = cls()
        
        # If it's the new format, load it directly
        if "environment" in data:
            instance.environment = data.get("environment", instance.environment)
            instance.physical_state = data.get("physical_state", instance.physical_state)
            instance.active_intentions = data.get("active_intentions", instance.active_intentions)
            
            # Load the new bridge variable
            instance.recent_action_bridge = data.get("recent_action_bridge", instance.recent_action_bridge)
        else:
            # Auto-migrate old legacy JSON saves so the app doesn't crash
            loc = data.get("location", "unknown location")
            pose = data.get("pose", "standing")
            
            # 3. Inject a generic temporal anchor for legacy saves so they 
            # don't instantly trigger the "Trope Trap" upon loading.
            instance.environment = f"[Time: Ongoing] {loc.capitalize()}."
            instance.physical_state = f"{pose.capitalize()}."
            
            instance.active_intentions = data.get("micro_intent", "None.")

        return instance

    def save_to_file(self, filepath: str):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_save_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> 'SceneState':
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return cls.from_dict(json.load(f))
            except json.JSONDecodeError:
                pass
        return cls()