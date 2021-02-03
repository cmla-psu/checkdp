int sparsevector(float q[], int size, float epsilon, float T, int NN)
{
  "TYPES: epsilon: <0, 0>; size: <0, 0>; q: <*, *>; T: <0, 0>; NN: <0, 0>";
  "PRECONDITION: ALL_DIFFER; ASSUME(NN > 0); ASSUME(NN <= size); ASSUME(T >= -10); ASSUME(T <= 10);";
  "CHECK: epsilon";

  int out = 0;
  float eta_1 = Lap(2 / epsilon);
  int T_bar = T + eta_1;
  int i = 0;
  int cost = epsilon * NN * 2;

  while (i < size)
  {
    float eta_3 = Lap(8 * NN / epsilon);
    // here sqrt(2) is approximated to be 2
    if (q[i] + eta_3 - T_bar >= 16)
    {
      CHECKDP_OUTPUT(q[i] + eta_3 - T_bar);
      cost = cost + epsilon;
    }
    else {
        float eta_2 = Lap(4 * NN / epsilon);
        if (q[i] + eta_2 - T_bar >= 0) {
          CHECKDP_OUTPUT(q[i] + eta_2 - T_bar);
          cost = cost + 2 * epsilon;
        }
        else
        {
          CHECKDP_OUTPUT(0);
        }
    }

    i = i + 1;
    if (cost > 4 * NN * epsilon - 2 * epsilon) {
        break;
    }
  }
}