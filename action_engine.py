import re
from collections import Counter
from typing import List, Dict, Any
from scene_state import SceneState

class ActionEngine:
    def apply_actions(self, state: SceneState, actions: List[Dict[str, Any]]) -> List[str]:
        execution_logs = []
        
        if not isinstance(actions, list):
            return ["Error: Expected a list of actions."]

        for action in actions:
            if not isinstance(action, dict):
                continue

            action_type = action.get("type", "")

            # The Nuke Protocol (Full Strip)
            if action_type in ["strip_all", "remove_all", "naked"]:
                state.physical_state = "Completely naked, stripped of all clothing."
                execution_logs.append("Stripped all clothing (Nuke applied).")

            # Goals & Intent
            elif action_type in ["set_goal", "set_intent", "update_intent"]:
                new_intent = action.get("intent") or action.get("goal")
                if new_intent and isinstance(new_intent, str):
                    state.active_intentions = new_intent 
                    execution_logs.append(f"Character intent updated.")
                    
        return execution_logs

    def get_banned_tics(self, recent_messages: List[Dict[str, Any]], history_depth: int = 10, repeat_threshold: int = 2) -> List[str]:
        """
        Analyzes the last N messages for overused action verbs (tics).
        Returns a list of action verbs that have been used too many times.
        """
        actions = []
        # Only look at the last `history_depth` messages
        for msg in recent_messages[-history_depth:]:
            content = msg.get('content', '')
            # Find all text inside asterisks (actions)
            matches = re.findall(r'\*([^*]+)\*', content)
            for match in matches:
                # Extract the first word of the action as the verb
                verb_match = re.search(r'^([a-zA-Z]+)', match.strip())
                if verb_match:
                    verb = verb_match.group(1).lower()
                    actions.append(verb)
        
        # Count frequencies
        freq = Counter(actions)
        # Return verbs that exceed or equal the threshold
        banned = [verb for verb, count in freq.items() if count >= repeat_threshold]
        return banned